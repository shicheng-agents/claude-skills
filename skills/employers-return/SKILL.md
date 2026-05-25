---
name: employers-return
description: Annual workflow for filing the Hong Kong IRD Employer's Return (BIR56A + IR56B forms) via eTAX. Runs once a year, typically April–May.
---

# /employers-return — BIR56A annual filing runbook

Hong Kong IRD issues the **Employer's Return (BIR56A)** to active employers around **1 April each year**, with submission deadline **1 month from date of issue** (so end-April to end-May depending on issue date). The company secretary usually sends a courtesy reminder a few weeks before. This skill is the end-to-end runbook for filing it via eTAX.

## Persistent reference data

These don't change between years and are reused in every filing. Verify against the latest BIR56A PDF anyway. Fill in once when adapting this skill.

| Field | Value |
|---|---|
| Employer's File Number | `<XXX-XXXXXXXX (N)(O)>` (from the most recent BIR56A) |
| Company (zh / en) | `<traditional Chinese name>` / `<English legal name>` |
| Registered address | `<as registered with the Companies Registry>` |
| Signing director | `<name, HKID, email, tel>` |
| Authorised eTAX account | `<director's eTAX login>` |
| IRD eTAX portal | https://www.ird.gov.hk/eng/ese/index.htm |
| IRD eTAX filing guide | https://www.gov.hk/en/apps/ird_er.htm |
| IRD confirmation sender | `e_alert@ird.gov.hk` |
| Storage in Drive | `{GDRIVE_REMOTE}:HR/BIR/<YEAR-RANGE>/` (e.g. `2025-26/`) |
| Salary breakdown source | `{DROPBOX_REMOTE}:<HR path>/IR56/<YEAR-RANGE>/IR56B breakdown <CY>.xlsx` (calendar-year keyed, not tax-year) |
| Bank statements | `{GDRIVE_REMOTE}:Accounting/Statements/Bank/<YYYY>/<prefix>_<YYYYMM>.pdf` |
| Tax year | 1 April – 31 March (IRD year) |

## Recurring counterparties (for cross-checking bank statements)

Maintain a small table mapping the staff/payments you expect to see on monthly bank statements, with the bank's reference patterns. Typical HSBC HKD savings statement patterns:

| Description | Account (recipient) | Reference pattern in HSBC |
|---|---|---|
| Salary to staff | `<their bank acct>` | `SALARYYYYYMM` |
| Place-of-residence rent (staff quarter) | `<landlord acct>` | `RENTYYYYMM` |
| MPF provider (e.g. Manulife) | — | `<PROVIDER NAME> <SCHEME NO>` |

## Workflow

### 1. Trigger

You arrive in any of three ways:
- BIR56A PDF lands in the accounting or director inbox from IRD (around April)
- Courtesy reminder from the company secretary — usually early-mid May
- This skill's scheduled reminder fires in April

### 2. Capture the form

Find the email (search `to:{ACCOUNTING_EMAIL} has:attachment employer's return newer_than:30d`). The forwarded PDF attachment is the BIR56A.

Download via `gog` (accounting inbox is the canonical scope):

```bash
docker exec {TOOLBOX} gog -a {ACCOUNTING_EMAIL} gmail attachment <threadId> <attachmentId> --output /work/BIR56A_<YEAR-RANGE>.pdf
```

Read the PDF and record:
- **S/N** (top-right, e.g. `S/N011322`)
- **File Number** — should match the persistent reference above
- **ERIC code** — labelled "ERIC (e-filing)" or "僱主確認碼 (電子報税)", e.g. `ER73CK65D6`. Only needed for new eTAX users; informational once the director has an active eTAX account.
- **Date of issue** and **deadline** (1 month after issue)

Upload to Drive at `{GDRIVE_REMOTE}:HR/BIR/<YEAR-RANGE>/BIR56A.pdf` (always literally `BIR56A.pdf`, no year suffix — match the convention from prior folders).

### 3. Compute IR56B figures for each employee

For each employee, you need: salary total + period of employment + place-of-residence info, for the tax year **1 April – 31 March**.

**Salary**: pull `IR56B breakdown <CY>.xlsx` from the HR archive. The file is keyed to **calendar year**, not tax year — you must extract the Apr–Dec rows from CY-N and combine with Jan–Mar from CY-(N+1) if that file exists.

**Cross-check against bank statements**:
- Pull the 12 monthly statements for the tax year from `{GDRIVE_REMOTE}:Accounting/Statements/Bank/`
- Match each salary line in the breakdown to a `CR TO <acct> SALARYYYYYMM` withdrawal
- Match each housing/quarter payment to a `CR TO <landlord acct> RENTYYYYMM` withdrawal
- If a payment doesn't appear in the bank statement, flag it (may have been paid in cash, via another account, or not paid)

If you have an existing `transactions_full.csv` from prior reconciliation work in `/work/`, that's the fastest source. Otherwise `scripts/parse_statements.py` (see the `/archivist` skill) rebuilds it from the PDFs.

### 4. Cessation / new hires

- **Cessation in-year**: should have triggered **Form IR56F** (notification of cessation) filed at the time, not bundled in the annual return. If IR56F was missed, the IR56B in the annual return still captures it via line 10 ("Period of employment for the year from X to Y").
- **New hires in-year**: should have triggered **Form IR56E** (commencement of employment) within 3 months. Verify before filing.
- **Place of residence stopped mid-year**: report on IR56B with the actual period it was provided.

### 5. File via eTAX

Best path is to **import last year's submitted data file** into a new return — it pre-populates personal data (HKID, addresses, spouse, capacity).

1. eTAX login as the signing director: https://www.ird.gov.hk/eng/ese/index.htm
2. Employer's Return e-Filing Services → start new return for the current year
3. Choose **"Import data from previously submitted Annual Return data file"**
4. Upload the `.dat` from last year's folder (`{GDRIVE_REMOTE}:HR/BIR/<PRIOR-YEAR>/ER-<FILE-NO>-BIR56A-<CY>.dat`)
5. Password to decrypt = the **prior year's TRN** (stored in that year's `ACKNOWLEDGEMENT.pdf` and `controlList.pdf`)
6. Update each IR56B with current year figures (salary, period of employment, place of residence). Remove employees who terminated and were already filed via IR56F in a prior year.
7. Update employer's postal address in BIR56A if it changed
8. Sign and submit (director)
9. On success, eTAX prompts to save a ZIP containing: `BIR56A.pdf`, `controlList.pdf`, `<EmployeeName>.pdf` per IR56B, `ACKNOWLEDGEMENT.pdf`, `ER-<FILE-NO>-BIR56A-<YYYY>.dat` (next year's import source)
10. IRD also emails a confirmation from `e_alert@ird.gov.hk` to the director inbox within minutes — contains check sum + filing timestamp (no TRN — TRN is only in the saved ZIP)

### 6. Archive submission

Upload the saved ZIP + the breakdown xlsx to Drive:

```bash
docker exec {TOOLBOX} rclone copy /local/ER-<FILE-NO>-BIR56A-<YYYY>.zip {GDRIVE_REMOTE}:HR/BIR/<YEAR-RANGE>/
```

Resulting folder should look like:

```
HR/BIR/<YEAR-RANGE>/
├── BIR56A.pdf                              ← issued by IRD
├── ER-<FILE-NO>-BIR56A-<YYYY>.zip          ← full submission bundle
├── ET-<FILE-NO>-BIR56A-<YYYY>-...sav       ← eTAX session save (optional)
└── IR56B breakdown <CY>.xlsx               ← input data
```

The ZIP unpacks to give you the .dat, controlList, ACKNOWLEDGEMENT, BIR56A signed, and per-employee IR56B PDFs — all of which you'd need for an IRD audit and as the import source for next year.

### 7. Distribute to employees

Per IRD instructions, the filed IR56B PDF must be **given to each employee** so they can use it for their salaries tax return. Email the individual IR56B PDF from the director's mailbox to each employee. Skip employees who already received it (e.g. the director themselves — though formally they should still be on record).

## Known quirks

- The `IR56B breakdown YYYY.xlsx` is keyed to **calendar year (Jan–Dec)** but the IR56B is **tax year (Apr–Mar)**. Always re-window the data before transcribing.
- Breakdown spreadsheets sometimes contain invalid dates (e.g. `31/11/YYYY`) — typos for end-of-month.
- The "Payment in HSBC" column in the breakdown is the **net payment** after MPF deduction; the IR56B salary figure is the **gross**. Confirm by checking the "Salary" column on the same row.
- The encrypted `.dat` file is a Java-serialized `hk.gov.ird.encryption.api.impl.EncryptedData` blob. It can only be opened by IRD's eTAX system using the prior year's TRN as the decryption key.
- Company secretaries often offer to file BIR56A for a fee (typically HK$800 + HK$400/employee) — this skill covers the workflow in-house.

## When to escalate to the user

- BIR56A not received by mid-April — chase IRD or the company secretary
- Bank statement reconciliation surfaces salary or rent payments not in the breakdown (or vice versa)
- An employee terminated in-year without an IR56F having been filed at the time
- The lease arrangement for a place of residence changed mid-year (start, stop, or moved)
- Any new hire during the year
