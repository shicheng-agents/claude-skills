---
name: ar-ap
description: AR aging + AP workflow for a multi-currency ERPNext company. Payment Reconciliation procedure (with the four classic FX-residual traps), invoice/PE workflow, mid-year dunning, and a per-counterparty reconciliation procedure when legacy JE-paid invoices need to be closed.
---

# /ar-ap — AR/AP runbook

This skill covers the **routine** AR/AP workflow plus the per-counterparty Payment Reconciliation procedure when reconciliation is needed mid-year. The complex year-end sweep across all major counterparties belongs to `/audit-reconciliation`.

## Scope split

| Workflow | Skill |
|---|---|
| Mid-year Payment Reconciliation on a single counterparty | `/ar-ap` (this skill) |
| Year-end Payment Reconciliation sweep across all counterparties | `/audit-reconciliation` |
| Booking new SIs / PIs | `/ar-ap` (this skill) |
| FCY revaluation on bank accounts | `/bookkeeper` |
| Source-doc retrieval (invoice PDFs, SWIFT slips) | `/archivist` |

## Counterparties (maintain this table when adapting)

### AR

| Customer | Currency | Status | Notes |
|---|---|---|---|
| `<Customer A>` | HKD | Active | Contact + relationship-owner |
| `<Customer B>` | CNY | Closed | Per-invoice cleanup completed YYYY-MM-DD; ledger 0/0 |

### AP

| Supplier | Currency | Status | Notes |
|---|---|---|---|
| `<Supplier A>` | EUR | Closed | All PIs reconciled; FX residual cleared via `voucher_type=Exchange Gain Or Loss` JE |
| Bank (TT fees) | HKD | Recurring | Booked to `Bank Service Charges - {ABBR}` |
| Company secretary, External auditor | HKD | Annual | Audit fee accrual via `/audit-reconciliation` |

## Access pattern

Same as `/bookkeeper` — `frappe.client.*` via bench `execute` for reads, `frappe.init` + direct Python for multi-step writes. See `/bookkeeper` SKILL.md for the boilerplate.

## Hard rules

1. **Default party currency**: a Customer/Supplier doc has `default_currency`; FCY invoices to that party must use it. Don't post a CNY supplier's PI in HKD.
2. **`outstanding_amount` is per-invoice, not per-party** — zeroing AP for a supplier means linking *every* PI to a PE or JE via Payment Reconciliation. The party GL net balance can be zero while individual PIs still show "Overdue".
3. **PE > JE for payments**: always prefer Payment Entry for new wires/transfers — keeps `outstanding_amount` self-zeroing. JE-based payment is a legacy-bookkeeper pattern; don't introduce new ones.
4. **Don't insert a PE without an invoice reference** unless it's an on-account advance. The `references` table is what closes `outstanding_amount`.
5. **Don't compute `outstanding_amount` manually** — use `erpnext.accounts.utils.get_payment_entry` helper via `run_doc_method` to build PE refs:
   ```bash
   $EB bench --site frontend execute "frappe.client.run_doc_method" --kwargs \
     '{"dt":"Sales Invoice","dn":"<SI-NAME>","method":"make_sales_invoice","args":{}}'
   ```

## Workflow A — Book a new payment (Payment Entry)

For a wire matching a single invoice (commonest case):

```python
import os; os.chdir("/home/frappe/frappe-bench/sites")
import frappe; frappe.init(site="frontend"); frappe.connect()
from erpnext.accounts.utils import get_payment_entry

pe = get_payment_entry(dt="Purchase Invoice", dn="<PI-NAME>")  # builds skeleton with refs

# Override the wire-specific fields
pe.posting_date = "YYYY-MM-DD"
pe.paid_from = "HSBC EUR Savings - {ABBR}"
pe.paid_amount = 6930.18                # actual FCY withdrawn (incl. TT fee)
pe.source_exchange_rate = 9.130744      # bank's published month-end rate
pe.references[0].allocated_amount = 6916.50   # FCY allocated to PI
# If wire is short of invoice by a fee/deduction:
pe.append("deductions", {"account": "Bank Service Charges - {ABBR}",
                          "cost_center": "Main - {ABBR}", "amount": 124.91})  # HKD = 13.68 × 9.130744

pe.insert(); pe.submit(); frappe.db.commit()
```

For the **Currency Exchange row at the PE date** — pre-check before submit; create if missing (ERPNext falls back to `frankfurter.dev` auto-fetch when `Currency Exchange Settings.disabled = 0`, but for audit defensibility you want documented bank-statement rates).

## Workflow B — Payment Reconciliation on a single counterparty

Use this when a counterparty has JE-paid invoices that still show "Overdue" in AR/AP reports despite the GL netting to zero.

### Step 1 — Diagnostic FIRST (mandatory; see Trap 3)

Query the party's net balance in both HKD and FCY *before* opening Payment Reconciliation:

```sql
SELECT party,
       ROUND(SUM(debit - credit), 2)                                  AS net_dr_hkd,
       ROUND(SUM(debit_in_account_currency - credit_in_account_currency), 2) AS net_dr_ccy
FROM `tabGL Entry`
WHERE account = '<Debtors or Creditors FCY account>'
  AND party = '<party name>'
  AND is_cancelled = 0
GROUP BY party;
```

- If **both** `net_dr_hkd = 0 AND net_dr_ccy = 0`: **stop**. There's nothing to reconcile. Any lingering UI rows are display-only Allocation artifacts. Running Reconcile will *manufacture* a phantom JE — see Trap 3 below.
- If both non-zero matching directions: proceed normally.
- If HKD ≠ 0 but FCY = 0: you're in Trap 4 territory — likely a prior partial-FX residual. Don't reconcile; post a follow-up Exchange-G/L JE instead (see Workflow C).

### Step 2 — Open Payment Reconciliation in UI

1. Navigate to Accounts → Payment Reconciliation
2. Set Party Type / Party / Receivable-Payable Account (the FCY account if party is FCY)
3. Click **Get Unreconciled Entries** → **Allocate**

### Step 3 — Override Difference Posting Date on every non-zero-difference row (Trap 1)

Default Difference Posting Date is the payment voucher's date (e.g. some old JE date) — backdates into closed FYs.

For each row showing a non-zero **Difference Amount**:
1. Click the pencil-edit icon
2. Set **Difference Posting Date** (`gain_loss_posting_date`) to current open FY (typically `YYYY-12-31` where YYYY is the FY being closed).
3. Confirm a `Currency Exchange` row exists for that date + currency pair.

Rows with `$0.00` difference don't need this — only ones with an FX gap.

### Step 4 — Reconcile

Click Reconcile. ERPNext posts an auto-generated JE for each FX difference. Check the JEs that get created:

```bash
$EB bench --site frontend execute "frappe.client.get_list" --kwargs \
  '{"doctype":"Journal Entry","filters":{"posting_date":"YYYY-12-31","is_system_generated":1},
    "fields":["name","total_debit","voucher_type","user_remark"],"order_by":"creation desc","limit_page_length":5}'
```

### Step 5 — Post-flight verification

Re-run the diagnostic SQL from Step 1.

- Both 0/0 → success.
- HKD ≠ 0 but FCY = 0 → you hit Trap 4 (partial FX residual). Proceed to Workflow C.
- Phantom self-ref rows still surface in PR UI → Trap 2. Run the PLE delink (Workflow D).

### Step 6 — Cancel phantom JEs if Trap 3 fired

If you accidentally clicked Reconcile on an already-clean account (and produced a phantom HKD-only JE on a zero-FCY account), cancel it:

```python
je = frappe.get_doc("Journal Entry", "ACC-JV-YYYY-NNNNN")
je.cancel(); frappe.db.commit()
```

Account goes back to clean 0/0.

## Workflow C — Clear an FX residual after partial-FX reconcile (Trap 4)

After PR or a JE-chain leaves `Creditors (FCY)` or `Debtors (FCY)` for a party at **FCY = 0 but HKD ≠ 0**, post:

```python
fix = frappe.get_doc({
    "doctype": "Journal Entry",
    "voucher_type": "Exchange Gain Or Loss",   # ← required for FCY=0 leg
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-12-31",
    "multi_currency": 1,
    "user_remark": "Clear HKD residual on <party> after Payment Reconciliation",
    "accounts": [
        # FCY party leg: only HKD value, no FCY change
        {"account": "<Creditors/Debtors FCY> - {ABBR}", "party_type": "<Supplier/Customer>",
         "party": "<party>",
         "debit": residual_hkd if hkd_negative_was_cr else 0,
         "credit": residual_hkd if hkd_positive_was_dr else 0,
         "debit_in_account_currency": 0, "credit_in_account_currency": 0,
         "account_currency": "<EUR/CNY/USD>", "exchange_rate": 1},
        # Offsetting Exchange G/L leg
        {"account": "Exchange Gain/Loss - {ABBR}",
         "debit": residual_hkd if hkd_positive_was_dr else 0,
         "credit": residual_hkd if hkd_negative_was_cr else 0,
         "account_currency": "HKD", "exchange_rate": 1},
    ],
})
fix.insert(); fix.submit(); frappe.db.commit()
```

## Workflow D — Delink self-ref PLE rows (Trap 2)

When legacy manual CNY/EUR-pair JEs (Cr X @ rate-A + Dr X @ rate-B, both refless) create Payment Ledger Entries with `voucher_no = against_voucher_no`, PR keeps surfacing them as phantom unreconciled rows.

Safety check first — confirm both legs net to zero in account currency:

```sql
SELECT SUM(amount_in_account_currency) AS net_ccy,
       SUM(amount)                      AS net_hkd
FROM `tabPayment Ledger Entry`
WHERE voucher_no = '<JE-name>'
  AND against_voucher_no = '<JE-name>'
  AND delinked = 0;
```

If `net_ccy = 0`, safe to delink. If non-zero, **stop and investigate** — those aren't pure self-cancelling pairs.

Then:

```python
import os; os.chdir("/home/frappe/frappe-bench/sites")
import frappe; frappe.init(site="frontend"); frappe.connect()

frappe.db.sql("""
    UPDATE `tabPayment Ledger Entry`
    SET delinked = 1
    WHERE voucher_no = %(v)s
      AND against_voucher_no = %(v)s
      AND party = %(p)s
      AND delinked = 0
""", {"v": "<JE-name>", "p": "<party name>"})
frappe.db.commit()
```

GL untouched; only sub-ledger reconciliation noise disappears.

## Workflow E — Book a new Purchase Invoice (when one arrives)

```python
pi = frappe.get_doc({
    "doctype": "Purchase Invoice",
    "supplier": "<supplier name>",
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-MM-DD",
    "bill_no": "<supplier's invoice number>",     # pre-check via get_list before insert
    "bill_date": "YYYY-MM-DD",
    "currency": "<EUR/USD/CNY/HKD>",
    "conversion_rate": <rate>,
    "update_stock": 1,                            # skip PR intermediation for drop-ship
    "items": [{"item_code": "<...>", "qty": <n>, "rate": <r>,
               "expense_account": "<P&L expense or COGS>"}],
})
pi.insert(); pi.submit(); frappe.db.commit()
```

**Critical for drop-ship / advance-pay flow**: use `update_stock=1` to skip Stock-Received-But-Not-Billed intermediation, which avoids the SRBNB residual class entirely.

**For supplier credits / return PIs**: book with `is_return=1` BUT manually change `expense_account` on each line from SRBNB to the correct account:
- Reversing a stocked PR → `Stock In Hand - {ABBR}`
- Reducing cost of already-sold goods → `Cost of Goods Sold - {ABBR}`
- Write-off → `Stock Adjustment - {ABBR}`

Pre-check via business key to avoid duplicates:

```bash
$EB bench --site frontend execute "frappe.client.get_list" --kwargs \
  '{"doctype":"Purchase Invoice","filters":{"bill_no":"<X>","supplier":"<Y>","docstatus":["!=","2"]},"fields":["name","grand_total","docstatus"]}'
```

## Workflow F — AR aging check (dunning)

Monthly or on-request, pull the AR aging report:

```bash
$EB bench --site frontend execute "frappe.desk.query_report.run" --kwargs \
  '{"report_name":"Accounts Receivable","filters":{"company":"{COMPANY_NAME}","report_date":"YYYY-MM-DD","ageing_based_on":"Posting Date"}}'
```

For invoices aging past 90 days:
1. Confirm not already paid (Payment Entry or JE against the relevant Debtors account)
2. Confirm no email correspondence resolving (search inboxes for `from:<customer> OR subject:<customer> newer_than:90d`)
3. **Don't draft a dunning email yourself** — escalate to the owner with the aging summary. The customer relationship owner handles it.

## Hard rules — reconciliation specific

1. **Trap 1 — Always override Difference Posting Date** per row on non-zero-difference rows.
2. **Trap 2 — Check for self-ref PLE rows** when PR keeps showing phantom unreconcileds.
3. **Trap 3 — Diagnostic first, always.** If party is already 0/0, don't reconcile.
4. **Trap 4 — Verify post-reconcile.** HKD-only residual on FCY-zero account → post Exchange-G/L cleanup JE.

## Output discipline

After any reconciliation or payment booking, report:
- `<doctype name> — <party> — <amount + currency> — <PE/JE name posted>`
- Confirmation: party net `<HKD x.xx> / <FCY y.yy>` (target: 0/0 or expected non-zero)
- Any cancelled artifact JEs by name

## When to escalate

- Reconcile would post to a closed/audited FY despite override (something's wrong with the FY config)
- Trap 4 residual is large (e.g. >HK$500) — may be a real economic loss, not just rate drift; flag for owner judgement
- AR invoice aging past 120 days — flag to owner, don't draft dunning
- Supplier sending duplicate invoice or pro-forma masquerading as final — flag
