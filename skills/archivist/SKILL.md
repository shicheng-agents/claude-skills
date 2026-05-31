---
name: archivist
description: Drive search, document retrieval, and ERPNext attachment for the company. Read-only by default — surfaces files from a Google Drive rclone remote without local copies. All rclone calls go via a dedicated toolbox container with direct egress, never from a VPN-routed agent container.
---

# /archivist — Drive search + source-doc retrieval runbook

The archivist's job is to **find** source documents (invoices, statements, contracts, SWIFT slips, audit packs) in `{GDRIVE_REMOTE}:` and either:
1. Quote relevant content back to the user (streaming, no local copy)
2. Attach the file to an ERPNext record (server-side flow)
3. Surface the canonical Drive path for the user to open directly

Read-only by default. Reorganising / moving Drive files is an explicit ask, not a default.

## ⚠ Critical egress rule

**All `rclone` calls go through the dedicated toolbox container `{TOOLBOX}`** (direct egress, low latency from your region). Never from the agent container — that may route through a distant VPN exit and add hundreds of ms latency / clog a shared exit node. See the parent CLAUDE.md "Network topology" section in your own deployment for the rule that applies to your setup.

```bash
TB="docker exec {TOOLBOX}"
```

## Disk discipline

Default to **streaming inspection** — never `rclone copy` to local unless you specifically need to OCR or post-process and have no streaming alternative.

| Goal | Pattern |
|---|---|
| Quick text peek | `$TB rclone cat {GDRIVE_REMOTE}:path/file.pdf \| strings \| head -50` |
| List a folder | `$TB rclone lsf {GDRIVE_REMOTE}:path/ --format pst --separator '\|'` |
| Recursive recent-only sweep | `$TB rclone lsf -R {GDRIVE_REMOTE}:path/ --max-age 4y --fast-list` |
| Server-side move within Drive | `$TB rclone moveto {GDRIVE_REMOTE}:A {GDRIVE_REMOTE}:B` |
| Server-side delete | `$TB rclone purge {GDRIVE_REMOTE}:DeadFolder --dry-run` (ALWAYS dry-run first) |
| Last-resort local copy | `$TB rclone copy {GDRIVE_REMOTE}:path/file.pdf /work/` → consume → `$TB rm /work/file.pdf` |

`/work/` is bind-mounted to `./work/` on host — visible to both containers. Use only for files that genuinely need local processing.

## Canonical Drive layout (example, adapt to your company)

```
{GDRIVE_REMOTE}:
├── Accounting/
│   ├── Statements/
│   │   ├── Bank/<YYYY>/<PREFIX>_<YYYYMM>.pdf      monthly bank statements
│   │   └── Card/<YYYY>/CC_<YYYYMM>.pdf            credit card statements
│   ├── Audit/FY<YYYY>/                            signed accounts + working papers
│   ├── Supplier-Invoices/<INV-CODE>/              one folder per supplier batch
│   └── ...
├── HR/
│   └── BIR/<YEAR-RANGE>/                          BIR56A.pdf + ER-...zip per year (HK)
├── Corporate/                                     BR cert, NAR1, incorporation docs (HK)
└── (other folders — see survey below)
```

Patterns to standardise:
- **Year folders**: calendar year (`2014/` … `2026/`) at each level
- **Statement naming**: `<PREFIX>_YYYYMM.pdf` (bank), `CC_YYYYMM.pdf` (card)
- **Audit folders**: `FY<YYYY>` matches the fiscal year
- **BIR folders**: tax year range like `2025-26/` (HK IRD convention)

## Scope: focus on recent activity

Recommend defaulting recursive sweeps to a `--max-age` window (e.g. 4 years) — legacy content gets deferred unless explicitly asked.

```bash
$TB rclone lsf -R {GDRIVE_REMOTE}: --format pst --separator '|' --max-age 4y --fast-list
```

## Workflow A — Find a document by counterparty + date

The most common request: "find the supplier X invoice batch Y" / "where's the bank statement for Dec 2024".

1. Start with the canonical layout — if it's a recurring doc (statement, audit), guess the path and `lsf`:

   ```bash
   $TB rclone lsf {GDRIVE_REMOTE}:Accounting/Statements/Bank/2024/ --format pst
   $TB rclone lsf {GDRIVE_REMOTE}:Accounting/Supplier-Invoices/ --format pst --dirs-only
   ```

2. If guessing fails, broaden via name search (Google Drive backend supports name search):

   ```bash
   $TB rclone lsf -R {GDRIVE_REMOTE}:Accounting/ --include "*<batch-id>*"
   $TB rclone lsf -R {GDRIVE_REMOTE}: --include "*BIR56A*" --max-age 4y
   ```

3. Stream-peek to confirm:

   ```bash
   $TB rclone cat {GDRIVE_REMOTE}:Accounting/Supplier-Invoices/<batch>/invoice.pdf | strings | head -100
   ```

4. Surface to user as a path string (don't download). User can click through in their Drive UI.

## Workflow B — Search by content (text inside PDFs)

rclone doesn't full-text search Google Drive content (only filename). For text search, use the Google Drive API via `gog` (the accounting mailbox is the canonical scope for Drive admin):

```bash
$TB gog --account {ACCOUNTING_EMAIL} drive search 'fullText contains "<term>"' --max 20
```

For non-PDF content already on Drive (Google Docs / Sheets), Drive's full-text index applies. PDFs are indexed too if Drive's OCR ran on them (most do).

If the user needs OCR on a scanned PDF that didn't index: that's a `--copy-to-/work/ + tesseract` job. Treat as a one-shot — clean up after.

## Workflow C — Attach a Drive file to an ERPNext record

ERPNext stores file URLs in `tabFile`. Two patterns:

**Option 1 — Public link only** (no content copy): paste the Drive shareable URL into the doc's `Comment` or set as `attached_to_field` if the doctype has a Data field for it. Simple, no transfer.

**Option 2 — Actual attachment** (stored in ERPNext's filesystem):

```python
import os; os.chdir("/home/frappe/frappe-bench/sites")
import frappe; frappe.init(site="frontend"); frappe.connect()

with open("/path/to/local/file.pdf", "rb") as f:
    content = f.read()

f = frappe.get_doc({
    "doctype": "File",
    "file_name": "<descriptive name>.pdf",
    "attached_to_doctype": "Purchase Invoice",
    "attached_to_name": "<PI-NAME>",
    "is_private": 1,
    "content": content,
})
f.insert(); frappe.db.commit()
```

For most cases, Option 1 is enough — Drive is the canonical archive, ERPNext just needs a pointer.

## Workflow D — Statement parsing for reconciliation

Bank and credit card PDFs parse via `scripts/parse_statements.py` (your repo will need this script — see the original script directory in the source repo). Auto-detects format. Run via the toolbox container (host may not have poppler):

```bash
$TB python3 /scripts/parse_statements.py /work/<folder> --csv /work/<folder>/transactions.csv
```

Flags:
- `--rename` (+`--style historical`) — canonicalise filenames to `<PREFIX>_YYYYMM.pdf` / `CC_YYYYMM.pdf`
- `--audit-from / --audit-to` — find missing months
- `--dry-run` — preview with `--rename`

CSV schema (DictReader-friendly):
```
statement_month, statement_end_date, account_number, sub_account, currency,
txn_date, post_date, description, deposit, withdrawal, balance,
fx_amount, fx_currency, fx_rate, source_file
```

Semantics: `deposit` = money IN, `withdrawal` = money OUT, both bank and CC.

Statements need to be *local* (in `/work/`) to parse. Pull one month at a time, parse, then delete:

```bash
mkdir -p /work/bank_<YYYYMM>/
$TB rclone copy {GDRIVE_REMOTE}:Accounting/Statements/Bank/<YYYY>/<PREFIX>_<YYYYMM>.pdf /work/bank_<YYYYMM>/
$TB python3 /scripts/parse_statements.py /work/bank_<YYYYMM>/ --csv /work/bank_<YYYYMM>/transactions.csv
# ... consume CSV ...
$TB rm -r /work/bank_<YYYYMM>/
```

For bulk parsing (all 12 months of a tax year for BIR reconciliation), see `/employers-return` — it has the pattern.

## Workflow E — Find a SWIFT slip for a wire payment

Wire slips typically live in supplier-named folders under `Accounting/Supplier-Invoices/`:

```bash
$TB rclone lsf -R {GDRIVE_REMOTE}:Accounting/Supplier-Invoices/ \
  --include "*SWIFT*" --include "*remittance*" --include "*<bank-prefix>*"
```

HSBC SWIFT slip filename pattern: `HK11<digits>BI<digits>.pdf`. The transaction reference (e.g. `HK111013BI433079`) appears both on the slip and as the bank-statement reference for the wire.

When a wire is booked but no SWIFT slip is in Drive, flag the gap to user. The slip can usually be re-pulled from the bank's online banking.

## Workflow F — Move / reorganise within Drive (explicit ask only)

Server-side moves cost no transit. Use `moveto` for one-off renames, `move` for folder-level shuffles:

```bash
# Single file
$TB rclone moveto {GDRIVE_REMOTE}:OldPath/file.pdf {GDRIVE_REMOTE}:NewPath/file.pdf

# Folder contents
$TB rclone move {GDRIVE_REMOTE}:OldFolder/ {GDRIVE_REMOTE}:NewFolder/

# Delete (ALWAYS dry-run first; never on untested destination)
$TB rclone purge {GDRIVE_REMOTE}:DeadFolder --dry-run
```

For statement-rename batches, prefer `scripts/parse_statements.py --rename` over manual `moveto` — it handles the canonical naming convention.

## Workflow G — Utility-bill email → Purchase Invoice (auto-ingest)

HK utility providers email a bill (usually with a PDF) each cycle to a dedicated mailbox.
The pipeline turns each into a **Draft Purchase Invoice** in ERPNext with the real provider
PDF attached. Canonical implementation: **`scripts/utility_bill_processor.py`** (run
`--dry-run` to preview, no args to process). This section documents its parsing **templates**
and the de-dup model so they can be maintained/ported — don't re-implement the 1.5k-line
script.

**Pipeline:** mailbox (via `gog`, since the utilities inbox is a Gmail account, *not* an
ERPNext Email Account) → list threads excluding `-label:Logged` → per-provider parse →
extract `{account, amount, bill_date, period, flat, utility_type, pdf}` → create Draft PI +
attach the **real** PDF + label the thread `Logged`.

### Per-provider parse templates

Each provider has a `parse_*` fn returning the same dict. Account # comes from the
subject/body; **amount** is provider-specific (`_extract_amount(text, provider_key)`); the
**`bill_date` MUST be re-extracted from the PDF** (the email arrival date is only a
placeholder). Anchors:

| Provider | `provider_key` | type | Account-# anchor | Period/date anchor |
|---|---|---|---|---|
| HK Electric | `provider_hk_electric` | electricity | subj `賬戶號碼:NNN` / `A/C No: NNN` | PDF `billDate=DD-MM-YYYY` / `dated DD/MM/YYYY`; `DD/MM/YYYY to DD/MM/YYYY` |
| CLP | `provider_clp` | electricity | subj `(NNNNN-NNNNN-N)` | subj `for MON, YYYY` |
| WSD | `provider_wsd` | water | `Account Number: NNNNNNNN` (8+ digits) | `DD/MM/YYYY to DD/MM/YYYY` |
| Towngas | `provider_towngas` | gas | `NNNN-NNNN-NN` / `Account: …` | period range |
| 3 Supreme | `provider_3supreme` | internet | `Account No: NNNNNN+` | period range |
| 1010 | `provider_1010` | internet | `Account No/Number NNNNNN+` | period range |
| SmarTone | `provider_smartone` | internet | `Account/AC No: NNNNNN+` | period range |
| HKBN | `provider_hkbn` | internet | `Account/A/C/No.: NNN` | — |

Adding a provider = a new `parse_<key>` returning that dict + an amount pattern in
`_extract_amount` + a sender in the mailbox query. Keep the regexes anchored to **subject +
body** (`combined = text + " " + subject`) because layouts drift.

### Guards before booking (don't create junk PIs)

- **Real PDF only.** If no genuine provider PDF is attached, **refuse** — do not fabricate a
  text-summary PDF (hard rule #8). Telecom "e-bill ready" mails often have no PDF → skip.
- **Billed-to check.** `_detect_billed_to` — a bill addressed to the **landlord** (not the
  company) must not be posted as a company PI (double-counts). Skip + flag.
- **Skippable subjects** are duplicates of the original bill: `Final Notice`, `Payment
  Overdue`, `Payment Reminder` → label `Logged`, don't book.

### Create the PI + attach evidence (via `erp`)

Deterministic key: **`bill_no = {SUPPLIER[:8].upper()}-{flat}-{bill_date}`** (e.g.
`HKELECT-KS-2026-05-27`). One PI per bill (so the provider PDF attaches to *that* PI):

```bash
erp insert --json '{"doctype":"Purchase Invoice","supplier":"HK Electric","bill_no":"HKELECT-KS-2026-05-27",
  "bill_date":"2026-05-27","items":[{"item_code":"Electricity","qty":1,"rate":1215.37,
  "expense_account":"<mapped acct>"}]}' --unique '[["bill_no","=","HKELECT-KS-2026-05-27"],["supplier","=","HK Electric"]]'
erp attach "Purchase Invoice" <PI-NAME> "<drive-url-of-bill.pdf>"
```

Use `erp data-import "Purchase Invoice" month.csv` only for a **batch** where per-PI PDF
evidence isn't needed — `data-import` attaches the source file to the *import doc*, not to
each PI, so for utility bills the per-bill `erp insert` + `erp attach` grain is correct.
(Account mapping + the $400/tenant electricity recharge cap are bookkeeping policy — see
`/bookkeeper`.)

### De-duplication — three layers (the cron is safe to re-run)

1. **Gmail label** — the query excludes `-label:Logged`; processed threads are labelled, so
   they never re-surface.
2. **Local state file** — `processed_thread_ids` is checked (`is_processed`) before work.
3. **PI existence guard** — `invoice_exists(bill_no, supplier)` queries ERPNext before
   insert; with the deterministic `bill_no` this prevents a double-create even if the label
   *and* state were lost. (`erp insert --unique` gives the same guard for ad-hoc loads.)

So the ingest is **both check-first and idempotent** — re-running creates nothing new.

## Hard rules

1. **No `rclone copy` to the agent container.** Toolbox only.
2. **No `rclone purge` without `--dry-run` first.**
3. **Don't act on legacy folders** without explicit ask.
4. **Don't bulk-copy to `/work/`** — single file or single month at a time.
5. **Don't OCR proactively** — Drive's built-in OCR is usually enough; user-driven for the rest.
6. **Don't attach Drive PDFs as ERPNext file objects routinely** — Drive is the canonical archive, ERPNext just needs the pointer (URL in a comment / remark).
7. **Don't `gog drive *` from the agent container** — same egress rule as rclone. Toolbox only.
8. **No synthetic / generated placeholder PDFs in the audit trail.** If an automated bill-ingest script can't find a real provider-issued PDF, it must NOT fabricate a 1.6KB "Bill Summary" PDF from email-body text and attach it as the source document. The right behaviour is to refuse to create the ERPNext record and alert the operator for manual entry. Reason: a synthesised PDF has the same filename pattern as a real one but no audit value — at year-end an auditor sampling source docs can't tell the two apart, and the underlying amount/date is at best email-body-grade reliable. (FY17-18 lesson: `utility_bill_processor.py` had created ~760 synthetic PDFs across CLP / WSD / 3SUPREME / SmarTone bills, making 25 % of the utility PI population unverifiable; the synthetic-PDF path is now gated behind `UTIL_ALLOW_NO_PDF=1` for narrow backfill use only.)

## Output discipline

When surfacing a file, give the user:
- **Full path**: `{GDRIVE_REMOTE}:Accounting/Supplier-Invoices/<batch>/<file>.PDF`
- **Modified date** (from `lsf --format pst`): tells the user how fresh / when filed
- **Size** (from `--format pst`): helps with "is this the right file" sanity check
- **One-line content gist** (from `rclone cat ... | strings | head`): just enough to confirm

Example output line:
```
Supplier X invoice batch 99 — {GDRIVE_REMOTE}:Accounting/Supplier-Invoices/X99/99_INVOICE.PDF
2025-12-12, 245 KiB. Invoice 99/CV, EUR 6,970.50, PO <PO-NAME>.
```

## Per-FY audit supporting documents package

When preparing a year's books for hand-off to a CPA, build a standardised package in Drive at `gdrive:FY<xx-yy> Audit Package/`. Eight-folder template:

| # | Folder | Contents |
|---|---|---|
| `01-Bank` | `HSBC/`, `BEA/`, `Elena-Reconstruction/` subfolders | Monthly statement PDFs + pre-categorised XLSX workbooks |
| `02-Sales-Invoices` | `<contract#>-<SURNAME>/` per guest | Per-guest INVOICES_FY<xx-yy>.csv (extracted from old-Xero archive) + email attachments (invoice PDFs, payment slip JPGs) |
| `03-Purchase-Invoices` | Per supplier or per category | Vendor bills/receipts |
| `04-Statutory` | `BR/`, `AR/`, `Audit/`, `Tax/`, `MPF/`, `Other/`, `Prior-Year-Audit/` | BR, NAR1, M&A, IR56B, prior-year-audited report |
| `05-License-Agreements` | Signed docx per contract | First-month split source of truth |
| `06-Check-Out-Forms` | Move-out reconciliations | Deposit refund tie-out |
| `07-Petty-Cash` | General + per-holder XLSX | Per-FY petty cash tracker workbooks |
| `99-Index` | INDEX.md + BANK_CONFIRMATION_STRATEGY.md (one-time) | Top-level index, open items, CPA gap list |

Build locally first in `~/erpnext/audit-package-fy<xx-yy>/`, then sync to Drive:

```bash
rclone copy ~/erpnext/audit-package-fy<xx-yy>/ "gdrive:FY<xx-yy> Audit Package/" -P
```

The INDEX.md is the cover sheet — it lists every file with its source path and FY relevance note, plus open items and known gaps for the CPA.

## Dispatch a subagent for statutory document collection

Statutory documents (BR cert, NAR1, M&A, IR56B, audit working papers, tax returns) are scattered across the Drive in multiple legacy locations. Manually finding them is slow. Instead, dispatch a Sonnet subagent with this template:

> *Scan all files across the Drive (`gdrive:` rclone remote) and grep for ~30 statutory keyword patterns. Download substantive documents to `~/erpnext/audit-package-fy<xx-yy>/04-Statutory/` organised into BR/AR/Audit/Tax/MPF/Other. Keywords: BR, business registration, annual return, NAR1, certificate of incorporation, profits tax, BIR51, BIR56, IR56, Companies Registry, MPF, audit, Tse Ka Wing, P06039, Curtis Mar, [employee names]. Write 04-Statutory/INDEX.md with per-file annotations + a Critical Gaps list (what's missing, where to source externally).*

The subagent typically pulls 70-100 files (~200 MB) and produces the INDEX.md in 10-15 minutes. The Critical Gaps list is the audit CPA's request list to the company.

## When to escalate

- Drive quota approaching its cap — flag to owner
- A file the user expects to find doesn't exist (and the request isn't "search and confirm absent")
- A folder layout mismatch (e.g. a month missing from the bank statements series) — surface gaps in monthly series proactively when doing reconciliation work
- Any request that would require downloading >100 MiB to local — confirm intent first
