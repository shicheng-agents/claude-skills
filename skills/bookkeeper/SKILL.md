---
name: bookkeeper
description: ERPNext bookkeeping runbook — journal entries, GL reconciliation, FX revaluation, FCY account hygiene, and ad-hoc adjustments outside the annual audit cycle. Focuses on keeping multi-currency books clean and reconcilable.
---

# /bookkeeper — ERPNext bookkeeping runbook

Books live in ERPNext (frappe 16.x + erpnext 16.x, default site `frontend`). This skill covers in-year bookkeeping that *isn't* the year-end audit reconciliation (see `/audit-reconciliation` for that): one-off JEs, GL reconciliations, FCY revaluations, credit card backfills, and ad-hoc cleanups.

## Scope split

| Workflow | Skill |
|---|---|
| Year-end auditor working-paper adjustments, PCV submission, ACB chain backfill | `/audit-reconciliation` |
| Annual IR56/BIR56A filing | `/employers-return` |
| AP wind-down on creditors, AR aging | `/ar-ap` |
| Source-doc retrieval + Drive search | `/archivist` |
| **Everything else GL-touching** | `/bookkeeper` (this skill) |

## Access pattern

```bash
EB="docker exec frappe_docker-backend-1"

# Read
$EB bench --site frontend execute "frappe.client.get" --kwargs '{"doctype":"...","name":"..."}'
$EB bench --site frontend execute "frappe.client.get_list" --kwargs '{"doctype":"...","filters":{...},"fields":[...]}'
$EB bench --site frontend execute "frappe.desk.query_report.run" --kwargs '{"report_name":"...","filters":{...}}'

# Write — use direct Python with frappe.init for anything that needs commit
$EB bench --site frontend execute "<dotted.path>" --kwargs '{...}'  # for single ops only
```

For anything multi-step or transactional (JE chains, FCY postings, cleanup scripts), drop into the backend container with `frappe.init`:

```python
docker exec -i frappe_docker-backend-1 bash -c '/home/frappe/frappe-bench/env/bin/python <<PY
import os; os.chdir("/home/frappe/frappe-bench/sites")
import frappe; frappe.init(site="frontend"); frappe.connect()
# ... your code ...
frappe.db.commit()
PY'
```

**Why `frappe.init` and not `bench console`**: bench-console exits before `frappe.db.commit()` fires, leaving submitted-looking docs that aren't actually persisted.

## Hard rules

1. **Never set `naming_series`** — let the server default fire. Counter doesn't roll back on delete.
2. **Posting date must fall in an open Fiscal Year**. Closed FYs (those with submitted PCV) require HKAS-8-style next-FY corrections.
3. **POST is not idempotent** — `frappe.client.insert` creates a duplicate doc every call. Pre-check by business key (`bill_no`, `po_no`, voucher_no) via `get_list` before insert.
4. **Multi-currency**: set `conversion_rate` explicitly on non-HKD documents.
5. **GL Entry is read-only**, derived from submitted vouchers. Never INSERT directly. Use Journal Entries to express adjustments.
6. **Submit is one-way** for `docstatus`. To amend a submitted doc, cancel → amend (creates `INV-001-1` sibling). Don't cancel submitted vouchers that have downstream allocations (PE refs, Payment Reconciliation links) — leave them, post a correcting JE in current FY.
7. **Watch `is_cancelled`** on GL queries — cancelled JEs flip `is_cancelled=1` on the GL but keep the doc. Always filter.
8. **Books Closing / Accounts Frozen Upto**: verify the site-wide setting before backdating. If set, posting into prior FYs is gated.

## Pre-flight checklist (before every JE)

1. **Fiscal Year open?** Confirm `posting_date` falls within an existing Fiscal Year *and* that FY's PCV is not submitted. If PCV exists, post in next-open-FY instead per HKAS 8.
2. **Currency Exchange row exists?** For multi-currency JEs, confirm a `Currency Exchange` doc exists for `(posting_date, from_currency, "HKD")`. If not, create one before submit. ERPNext falls back to `frankfurter.dev` auto-fetch when `Currency Exchange Settings.disabled = 0`, but auditors expect documented bank-statement rates at year-end.
3. **Reporting currency set?** `Company.reporting_currency = "HKD"`. If null, every multi-currency GL Entry fails on `set_amount_in_reporting_currency` with the misleading "HKD to None" error.
4. **HKD legs filled explicitly?** When using `voucher_type="Exchange Gain Or Loss"`, ERPNext skips the auto-populate from `debit_in_account_currency * exchange_rate`. Set `debit` / `credit` (HKD) fields explicitly on the HKD-account legs or the JE submits silently zero-amount and unbalanced.
5. **Distributable reserves test** (only for dividend / loan-to-director reclassification): RE Cr + FY-YTD profit pre-PCV − Dividends Paid Dr must be positive after the new entry. Companies Ordinance s.297 issue (HK) if negative — flag to owner.

## Common JE patterns

### Pattern A — Standard HKD-only adjustment

```python
adj = frappe.get_doc({
    "doctype": "Journal Entry",
    "voucher_type": "Journal Entry",
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-MM-DD",
    "user_remark": "<concise reason + cross-ref to source/email/JV ID>",
    "accounts": [
        {"account": "<Dr account> - {ABBR}", "debit_in_account_currency": X, "debit": X,
         "account_currency": "HKD", "exchange_rate": 1},
        {"account": "<Cr account> - {ABBR}", "credit_in_account_currency": Y, "credit": Y,
         "account_currency": "HKD", "exchange_rate": 1},
    ],
})
adj.insert(); adj.submit(); frappe.db.commit()
```

### Pattern B — Multi-currency JE (FCY payment, supplier credit, etc.)

Each non-HKD leg needs `account_currency`, `exchange_rate`, and `debit_in_account_currency` / `credit_in_account_currency` for the FCY amount. HKD legs as above.

```python
{"account": "Creditors (EUR) - {ABBR}", "party_type": "Supplier", "party": "<supplier name>",
 "credit_in_account_currency": 6970.50,   # FCY
 "credit": 63646.78,                       # HKD = FCY × exchange_rate
 "account_currency": "EUR",
 "exchange_rate": 9.130744},               # bank's published rate at posting date
```

### Pattern C — FCY revaluation JE (FCY balance unchanged, HKD valuation updated)

**Must use `voucher_type="Exchange Gain Or Loss"`** to bypass ERPNext's "Both Debit and Credit values cannot be zero" validation on FCY legs (FCY-amount = 0, HKD-amount = revaluation delta). And **set `debit` / `credit` (HKD) explicitly** on the HKD-account legs.

```python
rev = frappe.get_doc({
    "doctype": "Journal Entry",
    "voucher_type": "Exchange Gain Or Loss",       # ← critical
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-12-31",
    "multi_currency": 1,
    "is_system_generated": 1,
    "user_remark": "FY-end FCY revaluation per bank statement rates",
    "accounts": [
        {"account": "Exchange Gain/Loss - {ABBR}", "debit": T, "debit_in_account_currency": T,
         "account_currency": "HKD", "exchange_rate": 1},
        {"account": "HSBC EUR Savings - {ABBR}", "credit": A, "credit_in_account_currency": 0,
         "account_currency": "EUR", "exchange_rate": 1},   # only HKD credit, no FCY change
    ],
})
rev.insert(); rev.submit(); frappe.db.commit()
```

Prefer ERPNext's **Exchange Rate Revaluation** doctype over manual JEs for routine year-end FCY revaluation — it computes the deltas across all FCY accounts in one pass.

### Pattern D — Trade prepayment / deferred expense

Distinct accounts to set up in the chart:

| Account | Type | Use |
|---|---|---|
| `Prepayment - {ABBR}` | Asset, `account_type=Receivable` | Trade prepayments to suppliers (invoice not yet received) |
| `Deferred Expenses - {ABBR}` | Asset, under `Prepayment & Deposits - {ABBR}` group, `account_type=""` | Period-deferred expenses (multi-FY items, e.g. business-registration certs) |

Pattern: post Dr `Deferred Expenses` / Cr `<P&L expense>` at FY-end, reverse Dr `<P&L expense>` / Cr `Deferred Expenses` on first day of next FY (whole-line, no daily amortisation unless material).

## Account map (load-bearing accounts)

Set these up in your chart and keep names stable — most of the JE templates depend on them.

| Account | Type | Notes |
|---|---|---|
| `HSBC HKD Sav <acct-no> - {ABBR}` | Asset, Bank | HKD operating; primary |
| `HSBC HKD Cur <acct-no> - {ABBR}` | Asset, Bank | HKD current |
| `HSBC USD Savings - {ABBR}` | Asset, Bank | USD; bank stmt rate used at year-end |
| `HSBC EUR Savings - {ABBR}` | Asset, Bank | EUR; revalued at bank stmt rate |
| `HSBC CNY Savings - {ABBR}` | Asset, Bank | CNY; revalued at bank stmt rate |
| `Credit Card - {ABBR}` | Liability | HSBC Visa; stmt cycle ~28th-of-month, *not* calendar |
| `Stock Received But Not Billed - {ABBR}` | Liability, `account_type="Stock Received But Not Billed"` | Should be 0 at year-end. JEs *are* permitted (unlike `account_type="Stock"`). |
| `Stock In Hand - {ABBR}` / `Stock In Transit - {ABBR}` | Asset, `account_type="Stock"` | **Direct JE blocked** by `validate_account_for_perpetual_inventory`. Use Purchase Receipt or skip stock intermediation. |
| `Loan to Director - {ABBR}` | Asset | Should be 0 — see "Distributable reserves" below |
| `Dividends Paid - {ABBR}` | Equity (Dr) | Separate from Retained Earnings until auditor's Adj #1 rolls it in |
| `Retained Earnings - {ABBR}` | Equity (Cr) | Closes via PCV each FY |
| `Exchange Gain/Loss - {ABBR}` | P&L | All FX deltas land here |
| `Accruals (N/T) - {ABBR}` | Liability | Audit fee provisions; draw down when invoice arrives |
| `Auditors' Remuneration - {ABBR}` / `Other Consulting Fee - {ABBR}` / `Business Registration Fee - {ABBR}` | P&L | Recurring annuals |
| `Bank Service Charges - {ABBR}` | P&L | TT fees, drift writeoffs |
| `Cost of Goods Sold - {ABBR}` | P&L | Where SRBNB residuals offset |

Account suffix `- {ABBR}` is mandatory — there are sibling accounts for other entities in some doctype lookups when running multi-company on the same site.

## Recurring monthly tasks

### Credit card reconciliation

HSBC CC statement cycle ends ~28th of each month. Pull `{GDRIVE_REMOTE}:Accounting/Statements/Card/<YYYY>/CC_<YYYYMM>.pdf`, parse via `scripts/parse_statements.py` (see `/archivist`), compare against `Credit Card - {ABBR}` GL.

Recurring autopay items to map out for your company (examples):
- Cloud hosting subscriptions (USD)
- Google Workspace seats (USD)
- Cashback rebates (HKD, occasional Cr lines)
- Annual fee (HKD; often reversed the following month)

Backfill missing JEs per transaction (Pattern A); for the annual-fee reversal pair use matched Dr+Cr to net zero.

Drift writeoff: small (<HK$100) opening drift to `Bank Service Charges - {ABBR}` is acceptable; flag larger gaps.

### HKD bank reconciliation

Pull the monthly bank statement. Match every line in `HSBC HKD Sav/Cur - {ABBR}` GL against the statement. Build a reference-pattern map of recurring items (salary, rent, MPF, etc.) so reconciliation is mostly auto-match.

### FCY bank reconciliation

USD/EUR/CNY HSBC statements arrive monthly. Compare per-account FCY balance to ERPNext after each statement. Drift between book rate and statement rate is normal — accumulated drift is cleared at year-end via Exchange Rate Revaluation, not monthly.

If you see a **specific transaction** that posted at a wildly wrong FX rate (e.g. 7.83 USD/HKD for a posting where actual was 7.78), flag it: that's a rate-typo issue, not normal drift.

## Distributable reserves discipline (HK Companies Ordinance s.297)

Before posting any dividend (Cr `Dividends Paid`) or reclassifying a loan-to-director (Dr `Dividends Paid` / Cr `Loan to Director`):

```
Distributable reserves = (Retained Earnings Cr balance)
                        + (FY-YTD net profit, pre-PCV)
                        − (Dividends Paid Dr cumulative balance)
```

Must be **positive** after the new entry. If negative, Companies Ordinance s.297 issue — flag to owner before posting.

## Things that ARE NOT bookkeeper's job

- **PCV submission** → `/audit-reconciliation` (uses ACB-chain-aware procedure)
- **Audit adjustments mirroring** → `/audit-reconciliation`
- **Annual FX seeding from bank stmts** → `/audit-reconciliation` (uses `scripts/extract_fx_rates.sh`)
- **Payment Reconciliation for individual customers/suppliers** → `/ar-ap` (with traps procedure)
- **Drive search and source-doc retrieval** → `/archivist`
- **Email triage** → `/email`

## Output discipline

Every JE submitted by this skill should:
1. Have a `user_remark` that captures the *why* in one sentence + a cross-ref (source email thread, prior JV, bank stmt date, etc.).
2. Be summarised in the response back to the user as `<JV name> — <one-line reason> — Dr <X> / Cr <Y>`.
3. Trigger a memory update only if the pattern is novel (new account, new gotcha). Routine monthly CC backfills don't warrant a memory.

## ERPNext gotchas (must respect)

- **`docstatus`**: 0 Draft, 1 Submitted, 2 Cancelled. Submit is one-way. To change a submitted doc, cancel then *amend* (creates `INV-001-1` sibling — naming pattern visible in the data).
- **POST is not idempotent**: `frappe.client.insert` creates a duplicate doc every call. Pre-check by business key (`bill_no`, `po_no`, etc.) via `get_list` before insert.
- **Naming series**: don't set; let the server default fire. Counter doesn't roll back on delete (audit gaps).
- **Posting date**: must fall in an open Fiscal Year and past Books Closing / Accounts Frozen Upto.
- **Multi-currency**: set `conversion_rate` explicitly on non-HKD invoices.
- **`GL Entry`**: read-only, derived. Populated only on submit. Mandatory cost-center / dimension validations also fire only on submit.
- **Payment Entry references**: use `erpnext.accounts.utils.get_payment_entry` helper via `run_doc_method` to build refs. Don't compute outstanding amount manually.
- **Agent user**: should have role `Accounts User` (+ `Accounts Manager` for posting JEs / cancelling). `if_owner` perms cause silent empty list responses.
- **`Period Closing Voucher` cancel is heavy** — don't cancel a submitted PCV without explicit authorization. Late corrections go in next-open-FY as HKAS-8-style JE.
- **ERPNext v16 PCV submit may not post GL immediately** — see `/audit-reconciliation` for the workaround. Bookkeeper shouldn't be submitting PCVs but should know to check `GL Entry` count after if asked.
- **Broken `Account Closing Balance` chain symptom**: Balance Sheet shows non-zero balances on accounts that GL Entry SUM(credit-debit) reports as zero. If this happens, hand off to `/audit-reconciliation` for the backfill procedure — don't try to "fix" with corrective JEs.
