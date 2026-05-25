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

## Hard rules

1. **No `rclone copy` to the agent container.** Toolbox only.
2. **No `rclone purge` without `--dry-run` first.**
3. **Don't act on legacy folders** without explicit ask.
4. **Don't bulk-copy to `/work/`** — single file or single month at a time.
5. **Don't OCR proactively** — Drive's built-in OCR is usually enough; user-driven for the rest.
6. **Don't attach Drive PDFs as ERPNext file objects routinely** — Drive is the canonical archive, ERPNext just needs the pointer (URL in a comment / remark).
7. **Don't `gog drive *` from the agent container** — same egress rule as rclone. Toolbox only.

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

## When to escalate

- Drive quota approaching its cap — flag to owner
- A file the user expects to find doesn't exist (and the request isn't "search and confirm absent")
- A folder layout mismatch (e.g. a month missing from the bank statements series) — surface gaps in monthly series proactively when doing reconciliation work
- Any request that would require downloading >100 MiB to local — confirm intent first
