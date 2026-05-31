---
name: bank-reconciliation
description: Use for ONGOING (monthly) bank-statement reconciliation in ERPNext — matching bank deposits to outstanding rent invoices, creating Payment Entries from bank lines, handling the advance-billing cadence (rent invoiced ~1 month ahead), reconciling one cycle at a time, and (interim, while Xero is still the invoicing system) validating automatic matching against Xero's allocation. Distinct from /audit-reconciliation, which is the year-end auditor close.
---

# Bank reconciliation (monthly)

**Principle — bank-statement-driven.** The bank statement is the truth for cash received.
For each unreconciled deposit, find the outstanding invoice it pays and **create the
Payment Entry *from* the bank line** (`create_payment_entry_bts`), which reconciles it in one
step. Do **not** pre-create PEs and hope they match — that's a crutch that dies at cutover.
The durable loop has no dependency on the source invoicing system.

## Billing cadence — the thing that bites (verified statistically, 90%+ every year 2022-26)

Rent is **advance-billed**: an invoice **issued in month M is for month M+1's rent**, and is
paid around M→M+1 (e.g. **April rent is invoiced mid-March, paid late-Mar/April**).

Consequence: **to reconcile month X's payments, the invoices ISSUED in month X−1 must be
present.** Sync/import invoices by **billed period**, not issue date. A naïve "issue date >=
month X" window misses the invoices X's payments actually settle. (SleepHere `xero_si_sync.py`
has a `CUTOVER_PERIOD` filter that mirrors by billed period and skips rent for periods already
closed in the books — no double-count; daily cron uses `--since ~50 days`.)

## One cycle at a time

Reconcile a **single month, clear it, then the next**. If many months sit open at once, each
tenant has several open invoices and amount can't disambiguate which a deposit pays. With one
open invoice per tenant, the matcher is unambiguous. Don't let a backlog accumulate.

## The matcher (statement-only, high precision)

For a deposit, identify the invoice from the **statement alone**:
1. Parse the payer from `/ORDP/<name>` in the description (full name).
2. Resolve to **exactly one** customer (the customer's full name must appear in the payer
   string; ≥2 name tokens, ≤1 missing — avoids same-token collisions).
3. Take that customer's **unique exact-amount** open invoice. **Otherwise abstain** — never
   guess. (Single-open-regardless, closest-amount, oldest-of-several all dropped precision to
   ~0.68; strict exact stays ~0.86–1.0.)
- **`/EREF/` reference hints do NOT exist on receipts** (verified) — payer name is the only
  reliable signal. The `ROOM MONTH` refs are on *outgoing* landlord payments, not receipts.
- The matcher is **month-agnostic** (amount+name) — mismatches come from invoice *coverage*
  (wrong period synced), not the matcher.

## Apply

- Preview with `erp reconcile <BankAccount> [--from-date --to-date] [--exact]` (venetanji/
  erpnext-cli). The PE/JE matching SQL needs a posting-date window — pass `--match-from/--match-to`.
- Match a deposit either to an existing **Payment Entry** (exact amount+date) or, durably, to
  an **outstanding Sales Invoice** → create the PE from the bank txn (`create_payment_entry_bts`).
- **Suggest-then-confirm** until precision is trusted on a given month; high-precision
  (exact-amount, unique-name) matches can be auto-posted. Verify by querying state (the
  bench/`erp exec` exit code is unreliable — see erpnext-cli).

## Interim validation against the source system (while Xero is still invoicing)

Xero already holds the payment→invoice allocation, so use it as an **answer key** to grade the
blind matcher (NOT as a data source — it disappears at cutover):
- `xero_match_test.py` scores each statement-only match against Xero's allocation → precision
  / recall per month. Truth cached at `state/xero_pmt_truth.json`.
- Typical: ~0.9 precision, ~0.3 recall on a multi-month backlog snapshot; recall rises in
  steady state (one open invoice/tenant).

## State / boundary

- Reconciliation frontier is whatever the last close set (SleepHere: 2026-03-31, end of the
  rebuilt FY25-26; FY26-27 is a clean slate, all bank txns open).
- **Don't double-count across the close**: invoices for periods already closed must not be
  re-mirrored as new outstanding AR (the period-cutover filter handles this).

## Scripts (SleepHere)

| Script | Role |
|---|---|
| `accounting-bot/scripts/xero_si_sync.py` | mirror source invoices → ERPNext SIs (per-line accounts; billed-period cutover; idempotent on `xero_invoice_no`) |
| `accounting-bot/scripts/xero_payment_sync.py` | **interim crutch** — mirror source payments → PEs; retire at cutover |
| `accounting-bot/scripts/xero_match_test.py` | Xero-truth scoring harness for the blind matcher |
| `erp reconcile` (venetanji/erpnext-cli) | preview/drive ERPNext's Bank Reconciliation Tool |

## Common mistakes

- Syncing invoices by **issue date** for a month's payments → misses the advance-billed
  invoices (the actual offset bug). Use billed period.
- Letting **multiple months** sit open → amount-ambiguous matching.
- Matching a deposit **directly to an invoice by amount only** without resolving the payer →
  same-amount collisions. Resolve the name first.
- Treating the source system's **human-entered payments** as the long-term mechanism. They're
  an interim answer key; the durable flow creates PEs from the bank statement.
