---
name: audit-reconciliation
description: Year-end audit reconciliation runbook for a multi-currency ERPNext company. Mirrors the external auditor's working-paper adjustments back into ERPNext, seeds the Currency Exchange table from monthly bank statement rates, and produces a clean ERPNext close that matches the signed audit to the penny. Annual workflow, typically May–July for the preceding FY.
---

# /audit-reconciliation — year-end close ↔ auditor reconciliation runbook

Each year the external auditor produces signed audited accounts plus a set of working papers that include adjusting journals not present in ERPNext. ERPNext's books may be kept day-to-day by a separate bookkeeper using arbitrary FX rates and various classification quirks. This skill is the workflow for **bringing ERPNext into line with the audit** after the auditor signs, so that the next year's audit starts from books that already match.

## Audit firm context (fill in when adapting)

| Party | Role | Contact |
|---|---|---|
| **Audit partner** | Sign-off, escalation | `<partner@auditor.example>` |
| **Audit senior / manager** | Day-to-day fieldwork contact | `<senior@auditor.example>` |
| **Company secretary** | Coordinator, usually CC on all audit traffic | `<sec@secfirm.example>` |
| **Prior auditor(s)** | Note any handover from a previous firm | — |

Signed audit reports go to `{GDRIVE_REMOTE}:Accounting/Audit/FY{YYYY}/`. The full working-paper set is **not** automatically sent — must be requested.

## Working-paper schedule codes (typical HK audit convention)

Lead schedule references annotate each BS/P&L line with a working-paper code. Conventions vary by firm; the codes below are common but verify against what your auditor actually uses.

| Code | Subject |
|---|---|
| **F-1** | Fixed assets (plant & equipment) |
| **I-1** | Stocks (inventory) |
| **J-1** | Trade debtors |
| **K-1** | Cash and bank balances (per-account FCY breakdown + revaluation) |
| **L-1** | Trade creditors |
| **O-1** | Equity (share capital, RE) |
| **P-1** | Deposits and prepayments |
| **Q-prefix** (QI / QE) | Income / Expense lines (P&L) |
| **A 2-1** | Retained profits |
| **Audit adjustment** | The actual adjusting journals — usually 2–5 JEs |

**Always ask for the K-series and audit adjustment papers explicitly.** The lead schedule alone (sums only) does not show the per-bank revaluation amounts you need to mirror in ERPNext.

## Key principle: ERPNext FCY rates ≠ audit FCY rates

ERPNext uses whatever `conversion_rate` was on each transaction. The auditor typically revalues at year-end using **the bank's published monthly statement rate** for the December statement. With one important caveat:

> The auditor revalues **selectively**: currencies where the period drift is material (commonly EUR, CNY) are revalued to bank statement rate; currencies that are close enough to book (often USD) may be left at book. Don't assume "all FCY get revalued" — read the K-1 schedule carefully.

## Workflow

### Step 1 — Trigger

The audit cycle for FY{YYYY} starts around April-May of FY{YYYY+1}:
- Company secretary sends a "please provide audit documents" email
- The auditor signs the financial statements 2-3 months later
- The lead schedule + working papers are NOT sent unless asked

When you see the secretary's kickoff email or the auditor's "signed financial statements attached" email, queue the reconciliation work.

### Step 2 — Ask for the working papers

Draft an email to the audit senior (with the secretary and partner CC'd) asking for:

1. **K-1 cash schedule** (per-bank FCY breakdown + revaluation rates)
2. **Audit adjustment journal(s)** (the actual Dr/Cr entries the auditor posted)
3. **Other lead schedules** as needed (J/L/P/F)
4. **Bookkeeper's management accounts** the auditor worked from (useful cross-check)

Frame as: *"these are working files that already exist behind the signed audit — I need them to align ERPNext to your methodology before next year's fieldwork begins. Not requesting additional work."* Seniors sometimes send just the lead schedule first; partners are often more forthcoming with the full working set. Escalate to the partner if the senior stalls.

Save all received PDFs to `{GDRIVE_REMOTE}:Accounting/Audit/FY{YYYY}/` with descriptive names:
- `<Auditor>_Lead_Schedules_FY{YYYY}.pdf`
- `<Auditor>_Management_Audit_K1_FY{YYYY}.pdf`
- `<Bank>_<CardName>_Statement_{YYYY}-12-28.pdf`

### Step 3 — Decode the working papers

The audit adjustment paper typically contains 2 JEs (sometimes more for asset write-downs or accruals):

**Adj #1 — "Opening balance adjustment + dividend recognition"** (posted at opening of FY):
```
Dr Retained Earnings              ≈prior-period drift
   Cr Dividends Paid                            (FY-1 dividend amount, rolled into RE)
   Cr Exchange Gain/Loss                         (FY-1 closing-rate catch-up, treated as current FY gain)
```

This rolls the cumulative pre-FY dividends from ERPNext's separate "Dividends Paid" Dr account INTO Retained Earnings, *and* recognises any unbooked prior-period FX as current-year P&L. **The auditor's treatment is often HKAS-8-pragmatic, not HKAS-8-strict** — many SME audits put the prior-period catch-up through current P&L for simplicity under HKFRS for Private Entities, even though strict HKAS 8 would treat it as a retained-earnings reclassification. **Mirror the auditor's approach exactly** rather than re-deriving — what matters is that ERPNext ends up matching the signed accounts.

**Adj #2 — "Year-end balance translation"** (posted at year-end):
```
Dr Exchange Gain/Loss             (sum of FCY HKD revaluation)
   Cr <Bank> EUR Savings                        (revalues EUR balance at bank stmt rate)
   Cr <Bank> CNY Savings                        (revalues CNY balance at bank stmt rate)
```

Reduce ONLY the FCY accounts the auditor revalued; leave others (e.g. USD) untouched if the audit didn't move them (selective revaluation).

### Step 4 — Post the adjustments to ERPNext

For **Adj #1** (all HKD, no multi-currency):
- Standard JE, voucher_type `"Journal Entry"`
- posting_date = first day of the FY being closed (e.g. 2024-01-01 for FY2024)
- Simple insert/submit

For **Adj #2** (multi-currency, FCY HKD revaluation):
- **Must use voucher_type `"Exchange Gain Or Loss"` + `multi_currency: 1` + `is_system_generated: 1`** to bypass ERPNext's "Both Debit and Credit values cannot be zero" validation on the FCY legs (which need `credit_in_account_currency=0` because the FCY balance doesn't change, only its HKD valuation).
- **Set `debit` / `credit` (HKD) fields explicitly** on the HKD-account legs (Exchange Gain/Loss row). With this voucher_type, ERPNext skips the auto-populate from `debit_in_account_currency * exchange_rate`, so omitting the HKD value silently posts a zero-amount leg and leaves the JE unbalanced even at docstatus=1.

Canonical script pattern (use `frappe.init` + direct Python; bench-console exits before commit):

```python
import os; os.chdir("/home/frappe/frappe-bench/sites")
import frappe; frappe.init(site="frontend"); frappe.connect()

# Adj #1 (HKD)
adj1 = frappe.get_doc({
    "doctype": "Journal Entry",
    "voucher_type": "Journal Entry",
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-01-01",
    "user_remark": "Audit adjustment #1 (<Auditor> working paper): ...",
    "accounts": [
        {"account": "Retained Earnings - {ABBR}", "debit_in_account_currency": X, "debit": X, "account_currency": "HKD", "exchange_rate": 1},
        {"account": "Dividends Paid - {ABBR}", "credit_in_account_currency": Y, "credit": Y, "account_currency": "HKD", "exchange_rate": 1},
        {"account": "Exchange Gain/Loss - {ABBR}", "credit_in_account_currency": Z, "credit": Z, "account_currency": "HKD", "exchange_rate": 1},
    ],
})
adj1.insert(); adj1.submit(); frappe.db.commit()

# Adj #2 (multi-currency)
adj2 = frappe.get_doc({
    "doctype": "Journal Entry",
    "voucher_type": "Exchange Gain Or Loss",   # ← critical: bypasses FCY=0 validation
    "company": "{COMPANY_NAME}",
    "posting_date": "YYYY-12-31",
    "multi_currency": 1,
    "is_system_generated": 1,
    "user_remark": "Audit adjustment #2 (<Auditor> working paper): ...",
    "accounts": [
        {"account": "Exchange Gain/Loss - {ABBR}", "debit_in_account_currency": T, "debit": T, "account_currency": "HKD", "exchange_rate": 1},
        {"account": "<Bank> EUR Savings - {ABBR}", "credit": A, "account_currency": "EUR", "exchange_rate": 1},   # only HKD credit, no FCY change
        {"account": "<Bank> CNY Savings - {ABBR}", "credit": B, "account_currency": "CNY", "exchange_rate": 1},
    ],
})
adj2.insert(); adj2.submit(); frappe.db.commit()
```

After both JEs are posted, verify at FY-end posting date:
- Per-bank HKD balances match K-1 column
- Total cash matches audit BS line
- FY P&L Exchange G/L matches audit P&L
- RE balance + Dividends Paid → effective RE c/f matches audit

### Step 5 — Seed the Currency Exchange table forward

Run your repo's FX-extraction script (e.g. `scripts/extract_fx_rates.sh`) to pull month-end rates from monthly bank PDFs, then upsert them into ERPNext's `Currency Exchange` table.

**Seed forward-only** from the current-year-being-audited's January through latest available — never backfill into closed/audited periods. Past `conversion_rate` values are locked to documents/GL Entries anyway, but seeding noise into pre-audit years serves no purpose.

### Step 6 — Submit the FY Period Closing Voucher

Once Adj #1 + Adj #2 are submitted and ERPNext ties to audit:

- Create a `Period Closing Voucher` doctype:
  - `posting_date` = YYYY-12-31
  - `fiscal_year` = YYYY
  - `period_start_date` = YYYY-01-01
  - `period_end_date` = YYYY-12-31
  - `closing_account_head` = "Retained Earnings - {ABBR}"
- Submit. ERPNext closes all P&L accounts to RE in one shot.

### Step 7 — Verify and archive

Final reconciliation check: in ERPNext, query the Balance Sheet + P&L for the FY, compare line-by-line to the signed audit. Every material line should now match. Document the close in your notes / memory (per-FY close state).

## ERPNext gotchas (specific to year-end close)

- **`Period Closing Voucher` cancel is heavy** — touches 30+ GL entries. Don't cancel a submitted PCV without explicit authorization. Better path for late corrections: HKAS 8-style JE in the *next* open FY.
- **ERPNext v16 PCV submit may not post GL immediately**: in v16 (frappe 16.x + erpnext 16.x), `on_submit` creates a child `Process Period Closing Voucher` doc that runs the actual GL closing asynchronously. If `Accounts Settings.use_legacy_controller_for_pcv` is unset and the queue worker isn't processing reliably, the PCV submits with `docstatus=1` and `gle_processing_status="In Progress"` but produces **zero GL entries** silently. Fix: invoke `erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher.process_gl_and_closing_entries(pcv_doc)` directly after submit, OR set `use_legacy_controller_for_pcv=1` in Accounts Settings before submitting. Verify with `frappe.db.count("GL Entry", {"voucher_no": pcv.name, "is_cancelled": 0})` — should be one entry per non-zero P&L account plus the offsetting RE entry.
- **PCV field naming in v16**: `transaction_date` (NOT `posting_date` alone) — both fields exist but `posting_date` may not auto-populate if you only set the other. Belt-and-braces: set both `transaction_date` and `posting_date` to the FY-end date.
- **Submitted JE cancel cascades**: if you cancel a submitted JE that has GL entries, ERPNext flips `is_cancelled=1` on the GL but keeps the doc — your queries should filter `is_cancelled=0`.
- **Dividends Paid lives as a separate Equity Dr account** — not auto-rolled into RE until the auditor's Adj #1 does it. If you query RE alone for "what's the retained profit", you'll over-state by the cumulative dividend amount until that roll-up happens.
- **`Currency Exchange` table is a lookup, not retroactive**: changing rates here doesn't affect already-submitted documents. Past GL is locked. Only future documents and ERR runs use the new rates.
- **Books Closing / Accounts Frozen Upto**: confirm whether your site uses a frozen-accounts setting. Posting into prior FYs is normally gated only by Fiscal Year existence and PCV state, but a frozen-upto setting will block further.
- **Broken `Account Closing Balance` chain**: ERPNext's Balance Sheet reads opening balances from `tabAccount Closing Balance` rows aggregated across prior PCVs. Each PCV's ACB stores **only that year's GL activity** (not cumulative). When the chain is missing — e.g. PCVs submitted on older ERPNext versions that didn't generate ACB rows — the BS computes wrong openings for the next open FY. Symptom: Balance Sheet shows non-zero balances on accounts that GL Entry SUM(credit-debit) reports as zero. **Fix**: backfill ACB for each missing PCV in chronological order via direct call to `make_closing_entries(doc.get_account_closing_balances(), doc.name, doc.company, doc.period_end_date)`. Pure ACB INSERT, no GL changes, no PCV cancel needed. The `get_pcv_gl_entries()` call beforehand is required (sets in-memory attributes), but is read-only.

## Pre-audit hygiene (do this BEFORE sending books to auditor)

The two-JE auditor adjustment described above mirrors what the auditor actually does. But there's a class of cleanup that's the bookkeeper's job, not the auditor's — books-housekeeping that should happen *before* fieldwork starts so the auditor sees clean baseline numbers.

### Loan-to-director reclassification

A legacy bookkeeper may have booked dividend distributions as `Loan to Director - {ABBR}` (current asset) instead of `Dividends Paid - {ABBR}` (contra-equity), with remarks like "owner will pay back later". These should be reclassified before audit. Standard JE in next-open-FY:

```
Dr Dividends Paid - {ABBR}        [amount]
   Cr Loan to Director - {ABBR}              [amount]
posting_date = year-end of the FY being cleaned
```

The auditor's Adj #1 will subsequently roll Dividends Paid into RE. Do NOT retroactively touch the original loan JE — if it's in a closed/audited FY, leave the FY-Y balance and post the reclass in FY-Y+1.

Check before posting: distributable reserves at FY-end = (RE Cr) + (FY YTD profit, pre-PCV) − (Dividends Paid Dr). Must be positive after the additional dividend. Companies Ordinance s.297 issue (HK) if negative — flag to owner before posting.

### SRBNB cleanup

`Stock Received But Not Billed - {ABBR}` should be zero at FY-end if the PR/PI workflow is healthy. It rarely is. Common failure modes:

- Orphan Dr (PI without matching PR, drop-ship case): offset to COGS
- Phantom Cr (return PI with `expense_account=SRBNB` instead of Stock In Hand): reverse out to COGS (treat as cost-of-sales reduction)
- FX rate residual (PI vs PR at different rates): offset to Exchange G/L

Bundle all SRBNB residuals into one cleanup JE at year-end.

### Payment Reconciliation per major counterparty

For each customer/supplier where payments were booked as Journal Entries instead of Payment Entries (a common legacy pattern), run Payment Reconciliation BEFORE handing books to auditor. This zeroes per-invoice `outstanding_amount` and resolves any FX gaps between payment-date rates and invoice-date rates.

**Diagnostic before clicking Reconcile**: query the party's GL net balance in both HKD and FCY. If both are already 0/0, **don't reconcile** — PR may manufacture a phantom HKD-only adjustment from any lingering open Allocation rows.

Critical: **override "Difference Posting Date" per row** on any row with a non-zero Difference Amount — default backdates into closed FYs. Set to current open FY. See `/ar-ap` for the full Payment Reconciliation procedure including how to delink self-referential PLE artifacts from legacy manual CNY/EUR pairs.

**Verify post-reconcile**: re-query party net HKD + FCY. If HKD ≠ 0 but FCY = 0, the reconcile chain left a partial FX residual — post a follow-up `voucher_type="Exchange Gain Or Loss"` JE to route the residual HKD to Exchange G/L. See `/ar-ap` Workflow C.

### Audit fee accrual lifecycle

A standing audit fee provision tends to roll forward year-over-year and decouple from actual invoice flow if not actively drawn down:

- FY-Y close: provision booked Cr Accruals X for *next* year's audit work
- FY-Y+1 ~Sep-Nov: actual auditor invoice arrives and gets paid — but if expensed direct to P&L (Dr Aud Rem / Cr Bank) without drawing against the accrual, the provision stays stale on the BS
- Accrual rolls forward indefinitely, while P&L double-counts the expense across years

**Pre-audit fix (annual, at FY-end)**: two JEs dated FY-end:

1. **Release stale provision**: `Dr Accruals (N/T) [stale amount] / Cr Auditors' Remuneration [stale amount]`. The credit to Aud Rem reduces FY-current P&L expense — recognises the over-provision as a P&L release.
2. **New provision for the about-to-be-audited FY**: `Dr Auditors' Remuneration [est. audit fee] + Dr Other Consulting Fee [est. PTR fee] / Cr Accruals (N/T) [total]`. Split estimate by line.

Combined, these net the BS from stale-amount to fresh-amount and reset the recognition pattern.

**Going-forward**: when paying the actual auditor invoice, draw down against the accrual: `Dr Accruals (N/T) [provision amount] + Dr (any overrun) Aud Rem & Other Consulting / Cr <Bank> [invoice paid]`. This keeps the provision lifecycle clean.

**HK audit fee market benchmark (2026)** for a small trading SME: HK$10-18k audit + HK$3-5k PTR. Dormant company: HK$3-8k. Sub-HK$8k unrealistic for an active multi-currency entity. Adjust to your jurisdiction.

### Cleanup order before PCV

1. Reclassify any loan-to-director that should be dividends
2. Zero SRBNB via bundled cleanup JE
3. Run Payment Reconciliation for every customer/supplier with JE-paid history (apply the diagnostic-first procedure)
4. Delink any self-ref PLE residuals via direct DB update
5. Reset audit fee accrual: release stale + book new FY provision (two-JE pattern)
6. Verify Balance Sheet matches GL Entry SUM — if not, suspect broken ACB chain and backfill
7. Compute pre-audit P&L; this is what's emailed to the auditor as "books are ready"
8. Wait for audit working papers → post Adj #1 + Adj #2 (mirror exactly)
9. Submit PCV last

## Common pitfalls

1. **Posting an HKAS 8 prior-period RE adjustment when the auditor used current-year P&L catch-up.** The end-state RE matches, but per-bank closing balances differ from K-1 by the FY-specific revaluation amount. Always mirror the auditor's exact JE structure rather than re-deriving "more correct" treatment.
2. **Forgetting to set `debit`/`credit` HKD fields explicitly on the HKD-account leg of an "Exchange Gain Or Loss" voucher_type JE.** ERPNext will accept and submit a JE with `debit=0` on the supposed Dr leg — silently unbalanced GL.
3. **Trying to revalue a currency when the auditor didn't.** Cross-check Adj #2 line-by-line; only revalue the currencies the auditor revalued.
4. **Submitting the PCV before posting Adj #1 + Adj #2.** The PCV would then close ERPNext's pre-adjustment P&L, freezing the wrong numbers into RE. Always: adjustments → verify → PCV.
5. **Forgetting the credit-card-statement-vs-calendar-year-end gap.** Most card cycles end mid-/late-month, not Dec 31. There's always a few days of post-statement activity; audit treats small residuals as immaterial without comment.

## Audit-side rate conventions observed

- **EUR / CNY revaluation**: bank's published statement rate, sometimes lightly rounded (e.g. 8.079887 → 8.080000).
- **USD revaluation**: skipped if book ≈ stmt within a few HKD — be alert for "kept at book" exceptions.
- **CC balance**: bank's December statement balance, with a small immaterial timing gap to ERPNext's calendar-year-end ledger position.

## Forward-looking automation

Once **all** of:
1. Currency Exchange seeded from monthly bank statements
2. Year-end revaluation runs at 31-Dec via ERPNext's Exchange Rate Revaluation doctype using those rates
3. Dividends are booked routinely to RE (not parked in "Dividends Paid" account)

…are in place, the auditor's two-JE adjustment shrinks to zero. The K-1 reconciliation becomes a check rather than a correction, and the FY{YYYY+1} reconciliation work after sign-off becomes near-trivial. This is the destination state. Until then, expect 2–5 audit adjustments per year that need mirroring.

## Related skills

`/bookkeeper` (in-year JE patterns) · `/ar-ap` (Payment Reconciliation traps) · `/archivist` (audit-pack PDFs in Drive) · `/employers-return` (annual payroll filing — separate cycle)
