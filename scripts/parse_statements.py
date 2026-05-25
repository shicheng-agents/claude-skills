#!/usr/bin/env python3
"""parse_statements.py — parse HSBC statement PDFs (bank + credit card) into a unified CSV.

Supported formats (auto-detected per file):
  * HSBC Business Direct (HKD Current / HKD Savings / Foreign Currency Savings)
  * HSBC Credit Card (World Business MC, billed in HKD, with FX continuation lines)

Output CSV columns (consistent across both formats — empty where N/A):
  statement_month, statement_end_date, account_number, sub_account, currency,
  txn_date, post_date, description, deposit, withdrawal, balance,
  fx_amount, fx_currency, fx_rate, source_file

Deposit/withdrawal semantics (account-perspective, same for both):
  * deposit    = money INTO the account (bank: credits; card: payments + rebates + CR)
  * withdrawal = money OUT of the account (bank: debits; card: purchases)

Usage:
    python3 parse_statements.py                       # uses the folder this script is in
    python3 parse_statements.py /path/to/statements   # explicit folder
    python3 parse_statements.py --csv transactions.csv
    python3 parse_statements.py --rename
    python3 parse_statements.py --audit-from 2025-01 --audit-to 2026-04

Requirements: Python 3.8+, pdftotext (poppler-utils) on PATH.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

# ---------- shared helpers ----------

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6, "Jul": 7,
    "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

CCY_CODES = {"HKD", "USD", "EUR", "CNY", "GBP", "JPY", "AUD", "SGD", "CHF"}


def month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def month_range(start: str, end: str) -> list[str]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out, cur, last = [], date(sy, sm, 1), date(ey, em, 1)
    while cur <= last:
        out.append(month_str(cur))
        cur = next_month(cur)
    return out


def pdf_to_text(pdf: Path) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            check=True, capture_output=True, text=True,
        )
        return proc.stdout
    except FileNotFoundError:
        sys.stderr.write("ERROR: pdftotext not found on PATH. Install poppler-utils.\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"ERROR extracting {pdf.name}: {e.stderr}\n")
        return ""


def parse_amount(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    is_neg = s.endswith("DR") or s.endswith("dr") or s.endswith("-")
    s = s.rstrip("DRdr- ").strip()
    try:
        v = float(s.replace(",", ""))
        return -v if is_neg else v
    except ValueError:
        return None


# ---------- data classes ----------

@dataclass
class Transaction:
    date_iso: str
    post_date_iso: str
    description: str
    currency: str
    deposit: Optional[float]
    withdrawal: Optional[float]
    balance: Optional[float]
    sub_account: str
    fx_amount: Optional[float] = None
    fx_currency: str = ""
    fx_rate: Optional[float] = None


@dataclass
class SubAccount:
    name: str
    currency: str
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    transactions: list[Transaction] = field(default_factory=list)


@dataclass
class Statement:
    path: Path
    end_date: date
    account_number: str
    format: str  # "bank" | "credit-card"
    sub_accounts: list[SubAccount] = field(default_factory=list)
    md5: str = ""

    @property
    def month_key(self) -> str:
        return month_str(self.end_date)


# ---------- format detection ----------

RE_BANK_HEADER = re.compile(r"HSBC Business Direct Statement", re.IGNORECASE)
RE_CC_HEADER = re.compile(r"World Business MC|Credit limit\s*\n?\s*HKD", re.IGNORECASE)


def detect_format(text: str) -> str:
    if RE_CC_HEADER.search(text):
        return "credit-card"
    if RE_BANK_HEADER.search(text):
        return "bank"
    # Fallback heuristics on filename / other markers
    if "Credit Card" in text or re.search(r"\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b", text):
        return "credit-card"
    return "bank"


# ====================== BANK STATEMENT PARSER ======================

RE_STMT_DATE_LONG = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b"
)
RE_ACCOUNT = re.compile(r"Number\s*[:：]?\s*(\d{3}-\d{6}-\d{3})")
RE_SHORT_DATE = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")
# End-of-line amount; require whitespace (or BOL) before digits to avoid matching
# fragments inside SWIFT tags like "/OCMT/EUR92008.29".
RE_AMOUNT_EOL = re.compile(r"(?:^|(?<=\s))-?\d{1,3}(?:,\d{3})*\.\d{2}(?:\s*DR)?$")
RE_AMOUNT_ANYWHERE = re.compile(r"(?:^|(?<=\s))-?\d{1,3}(?:,\d{3})*\.\d{2}(?:\s*DR)?(?=\s|$)")
RE_SWIFT_TAG = re.compile(r"^/[A-Z]{2,6}/")


def _bank_detect_meta(text: str, path: Path) -> tuple[date, str]:
    m = RE_STMT_DATE_LONG.search(text)
    if not m:
        raise ValueError(f"{path.name}: could not find statement date")
    d = date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    am = RE_ACCOUNT.search(text)
    return d, (am.group(1) if am else "UNKNOWN")


def _bank_split_blocks(text: str) -> list[tuple[str, str]]:
    """Slice text into per-sub-account blocks; dedupe continuation pages."""
    marker = re.compile(
        r"HSBC Business Direct (HKD Current|HKD Savings|Foreign Currency Savings|HKD Time Deposits?|Foreign Currency Time Deposits?)\b"
    )
    stop_re = re.compile(r"Special Privileges|Exchange Rate|HSBC Business Direct Statement")
    positions = [(m.start(), m.group(1)) for m in marker.finditer(text)]
    if not positions:
        return []
    stops = [m.start() for m in stop_re.finditer(text)]
    raw: list[tuple[str, str]] = []
    for i, (pos, name) in enumerate(positions):
        nxt = positions[i + 1][0] if i + 1 < len(positions) else None
        end_candidates = [s for s in stops if s > pos]
        end = nxt if nxt is not None else (min(end_candidates) if end_candidates else len(text))
        if nxt is not None:
            end = min(end, nxt)
        raw.append((name, text[pos:end]))
    # Keep longest block per sub-account (the real one, vs the continuation header)
    best: dict[str, tuple[str, str]] = {}
    for name, block in raw:
        if name not in best or len(block) > len(best[name][1]):
            best[name] = (name, block)
    return list(best.values())


def _find_column_positions(header_line: str) -> dict[str, int]:
    """Find x-positions of Deposit / Withdrawal / Balance columns in the header.
    Returns the *end* position of each header word, which is the right-edge of
    the column (amounts are right-aligned to this)."""
    cols: dict[str, int] = {}
    for label in ("Deposit", "Withdrawal", "Balance"):
        i = header_line.find(label)
        if i >= 0:
            cols[label] = i + len(label)
    return cols


def _assign_by_column(line: str, amounts_with_pos: list[tuple[float, int]],
                      cols: dict[str, int], desc: str
                      ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Place each amount into deposit/withdrawal/balance based on its right-edge x-position
    relative to the column header right-edges. Falls back to keyword heuristic if cols missing."""
    deposit = withdrawal = balance = None
    if not cols or "Deposit" not in cols or "Withdrawal" not in cols or "Balance" not in cols:
        return _assign_by_keyword(amounts_with_pos, desc)
    tol = 6  # characters of slack on either side
    for val, rpos in amounts_with_pos:
        best_label = None
        best_dist = None
        for label, col_end in cols.items():
            d = abs(rpos - col_end)
            if d <= tol and (best_dist is None or d < best_dist):
                best_dist, best_label = d, label
        if best_label is None:
            # Couldn't snap to a column — last-resort keyword fallback for this single amt
            d2, w2, b2 = _assign_by_keyword([(val, rpos)], desc)
            deposit = deposit if deposit is not None else d2
            withdrawal = withdrawal if withdrawal is not None else w2
            balance = balance if balance is not None else b2
            continue
        if best_label == "Deposit":
            deposit = val
        elif best_label == "Withdrawal":
            withdrawal = val
        elif best_label == "Balance":
            balance = val
    return deposit, withdrawal, balance


def _assign_by_keyword(amounts_with_pos: list[tuple[float, int]], desc: str
                       ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Fallback when column positions can't be determined."""
    amounts = [v for v, _ in amounts_with_pos]
    deposit = withdrawal = balance = None
    if len(amounts) == 1:
        u = desc.upper()
        if "B/F BALANCE" in u or "BALANCE B/F" in u or "C/F BALANCE" in u or "BALANCE C/F" in u:
            balance = amounts[0]
        else:
            withdrawal = amounts[0]
    elif len(amounts) == 2:
        first, last = amounts
        u = desc.upper()
        is_credit = any(k in u for k in (
            "CREDIT INTEREST", "INTEREST CREDITED", " CR ", "DEPOSIT", "INWARD",
            "REFUND", "CASH REBATE",
        ))
        if is_credit:
            deposit, balance = first, last
        else:
            withdrawal, balance = first, last
    elif len(amounts) >= 3:
        deposit, withdrawal, balance = amounts[-3], amounts[-2], amounts[-1]
    return deposit, withdrawal, balance


def _extract_amounts_with_pos(line: str) -> tuple[str, list[tuple[float, int]]]:
    """Extract trailing amounts together with their right-edge column position.
    Returns (line_without_amounts, [(value, right_edge_pos), ...])."""
    amounts_with_pos: list[tuple[float, int]] = []
    work = line
    # We scan right-to-left, finding amounts that sit at the end of the (trimmed) string.
    while True:
        stripped = work.rstrip()
        if not stripped:
            break
        m = RE_AMOUNT_EOL.search(stripped)
        if not m or m.end() != len(stripped):
            break
        val = parse_amount(m.group(0))
        if val is None:
            break
        rpos = len(stripped)  # right-edge column position in the original line
        amounts_with_pos.insert(0, (val, rpos))
        work = stripped[: m.start()].rstrip()
    return work, amounts_with_pos


def _parse_bank_sub(name: str, block: str, statement_end: date) -> SubAccount:
    is_fcy = "Foreign Currency" in name
    currency = "MIXED" if is_fcy else "HKD"
    sub = SubAccount(name=name, currency=currency)
    lines = block.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if "Transaction Details" in ln and "Deposit" in ln and "Withdrawal" in ln and "Balance" in ln:
            header_idx = i
            break
    if header_idx is None:
        return sub
    col_positions = _find_column_positions(lines[header_idx])

    pending_desc: list[str] = []
    current_date: Optional[date] = None
    current_currency: Optional[str] = None
    for ln in lines[header_idx + 1:]:
        s = ln.strip()
        if s.startswith("Total No. of Deposits") or s.startswith("Total Deposit Amount"):
            break
        if not s:
            continue
        raw = ln.rstrip()
        ccy_at_start: Optional[str] = None
        rest_offset = 0
        rest = raw
        if is_fcy:
            head = s.split(None, 1)
            if head and head[0] in CCY_CODES:
                ccy_at_start = head[0]
                # Re-find the rest in the original `raw` line (keep column positions)
                idx = raw.find(head[0])
                rest_offset = idx + len(head[0])
                rest = raw[rest_offset:]
        m = RE_SHORT_DATE.match(rest.lstrip())
        if m:
            lstrip_offset = len(rest) - len(rest.lstrip())
            day = int(m.group(1))
            mon = MONTHS[m.group(2)]
            year = statement_end.year if mon <= statement_end.month else statement_end.year - 1
            current_date = date(year, mon, day)
            if ccy_at_start:
                current_currency = ccy_at_start
            elif not is_fcy:
                current_currency = currency
            pending_desc = []
            tail_start = rest_offset + lstrip_offset + m.end()
            tail = raw[:tail_start].ljust(tail_start) + raw[tail_start:]  # preserve positions
            # Simpler: amounts live in raw; we just need to extract from `tail` portion
            text_part, amts = _extract_amounts_with_pos(raw[tail_start:])
            # Adjust amounts' positions to original line's coordinate space
            amts = [(v, p + tail_start) for v, p in amts]
            if text_part.strip():
                pending_desc.append(text_part.strip())
            if amts:
                desc = " | ".join(x for x in pending_desc if x).strip() or "(no description)"
                deposit, withdrawal, balance = _assign_by_column(raw, amts, col_positions, desc)
                # Override for B/F balance lines
                if len(amts) == 1 and ("B/F BALANCE" in desc.upper() or "BALANCE B/F" in desc.upper()):
                    deposit = withdrawal = None
                    balance = amts[0][0]
                sub.transactions.append(Transaction(
                    date_iso=current_date.isoformat(), post_date_iso=current_date.isoformat(),
                    description=desc, currency=current_currency or currency,
                    deposit=deposit, withdrawal=withdrawal, balance=balance,
                    sub_account=sub.name,
                ))
                pending_desc = []
        else:
            if ccy_at_start and is_fcy:
                current_currency = ccy_at_start
            line_for_parse = rest if is_fcy else raw
            if RE_SWIFT_TAG.match(line_for_parse.strip()):
                if current_date is not None:
                    pending_desc.append(line_for_parse.strip())
                continue
            text_part, amts = _extract_amounts_with_pos(line_for_parse)
            if is_fcy and rest_offset:
                amts = [(v, p + rest_offset) for v, p in amts]
            if current_date is None:
                continue
            if text_part.strip():
                pending_desc.append(text_part.strip())
            if amts:
                desc = " | ".join(x for x in pending_desc if x).strip() or "(no description)"
                deposit, withdrawal, balance = _assign_by_column(raw, amts, col_positions, desc)
                if len(amts) == 1 and ("B/F BALANCE" in desc.upper() or "BALANCE B/F" in desc.upper()):
                    deposit = withdrawal = None
                    balance = amts[0][0]
                sub.transactions.append(Transaction(
                    date_iso=current_date.isoformat(), post_date_iso=current_date.isoformat(),
                    description=desc, currency=current_currency or currency,
                    deposit=deposit, withdrawal=withdrawal, balance=balance,
                    sub_account=sub.name,
                ))
                pending_desc = []

    # balance-flow verification: where deposit/withdrawal got auto-assigned but
    # the balance flow disagrees, swap them. Catches column-alignment edge cases.
    _verify_balance_flow(sub)

    bf = next((t for t in sub.transactions if t.description.upper().startswith("B/F BALANCE")), None)
    if bf:
        sub.opening_balance = bf.balance
    last_bal = next((t for t in reversed(sub.transactions) if t.balance is not None), None)
    if last_bal:
        sub.closing_balance = last_bal.balance
    return sub


def _verify_balance_flow(sub: SubAccount) -> None:
    """Sanity-check: for each (txn, prev_balance), expected = prev + deposit - withdrawal.
    If a mismatch matches what swapping deposit↔withdrawal would fix, swap and warn."""
    prev_balance: Optional[float] = None
    prev_currency: Optional[str] = None
    for t in sub.transactions:
        # FCY sub-accounts run multiple currencies in one block — balance flow only
        # holds within a single currency stream.
        if prev_currency != t.currency:
            prev_balance = t.balance
            prev_currency = t.currency
            continue
        if prev_balance is None or t.balance is None:
            prev_balance = t.balance if t.balance is not None else prev_balance
            continue
        d, w = t.deposit or 0.0, t.withdrawal or 0.0
        expected_as_is = prev_balance + d - w
        expected_swapped = prev_balance - d + w
        # Within 0.01 of expected → OK
        if abs(expected_as_is - t.balance) <= 0.01:
            pass
        elif abs(expected_swapped - t.balance) <= 0.01 and (d or w):
            # swap deposit and withdrawal
            t.deposit, t.withdrawal = t.withdrawal, t.deposit
            sys.stderr.write(
                f"  [balance-flow swap] {t.sub_account} {t.date_iso} {t.description[:50]} d/w corrected\n"
            )
        prev_balance = t.balance


def _parse_bank(path: Path, text: str) -> Statement:
    end_date, acct = _bank_detect_meta(text, path)
    stmt = Statement(path=path, end_date=end_date, account_number=acct, format="bank")
    stmt.md5 = hashlib.md5(path.read_bytes()).hexdigest()
    for name, block in _bank_split_blocks(text):
        stmt.sub_accounts.append(_parse_bank_sub(name, block, end_date))
    return stmt


# ====================== CREDIT CARD PARSER ======================

RE_CC_STMT_DATE = re.compile(
    r"Statement date\s+Statement balance\s*\n.*?(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(20\d{2})\b",
    re.IGNORECASE | re.DOTALL,
)
RE_CC_STMT_DATE_FALLBACK = re.compile(
    r"\b(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(20\d{2})\b"
)
RE_CC_CARD_NUMBER = re.compile(r"\b([3-6]\d{3})\s+(\d{4})\s+(\d{4})\s+(\d{4})\b")
RE_CC_TXN = re.compile(
    r"^(?P<post>\d{2}[A-Z]{3})\s+(?P<trans>\d{2}[A-Z]{3})\s+(?P<rest>.+?)\s+(?P<amount>[\d,]+\.\d{2})(?P<cr>CR)?\s*$"
)
RE_CC_PREV_BALANCE = re.compile(r"^\s*PREVIOUS BALANCE\s+([\d,]+\.\d{2})", re.MULTILINE)
RE_CC_FX_RATE = re.compile(r"\*EXCHANGE RATE:\s*([\d.]+)")
RE_CC_FX_INLINE = re.compile(
    r"\b(USD|EUR|GBP|JPY|CNY|AUD|SGD|CHF)\s+([\d,]+\.\d{2})"
)


def _cc_detect_meta(text: str, path: Path) -> tuple[date, str]:
    m = RE_CC_STMT_DATE.search(text)
    if not m:
        m = RE_CC_STMT_DATE_FALLBACK.search(text)
    if not m:
        raise ValueError(f"{path.name}: could not find credit card statement date")
    d = date(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)))
    cm = RE_CC_CARD_NUMBER.search(text)
    if cm:
        card = "-".join([cm.group(1), cm.group(2), cm.group(3), cm.group(4)])
    else:
        card = "UNKNOWN"
    return d, card


def _cc_short_to_date(short: str, statement_end: date) -> date:
    """'02JAN' on a statement dated 27 JAN 2026 → 2026-01-02.
    Wrap-around: a Dec txn on a Jan-dated statement → previous year."""
    day = int(short[:2])
    mon = MONTHS[short[2:].upper()]
    year = statement_end.year if mon <= statement_end.month else statement_end.year - 1
    return date(year, mon, day)


def _parse_cc(path: Path, text: str) -> Statement:
    end_date, card = _cc_detect_meta(text, path)
    stmt = Statement(path=path, end_date=end_date, account_number=card, format="credit-card")
    stmt.md5 = hashlib.md5(path.read_bytes()).hexdigest()
    sub = SubAccount(name="Credit Card", currency="HKD")
    # opening balance from PREVIOUS BALANCE line
    pb = RE_CC_PREV_BALANCE.search(text)
    if pb:
        sub.opening_balance = parse_amount(pb.group(1))

    running_balance = sub.opening_balance
    lines = text.splitlines()
    pending: Optional[Transaction] = None  # for FX continuation
    pending_fx_inline: Optional[tuple[str, float]] = None  # (ccy, amt) captured on the txn line itself

    for raw in lines:
        ln = raw.rstrip()
        s = ln.strip()
        if not s:
            continue

        # Stop when we hit footer / disclaimer sections (transaction list ends).
        if (s.startswith("*For credit card transactions") or
                s.startswith("Note:") or s.startswith("REWARDCASH ") or
                s.startswith("REWARDS OF YOUR CHOICE") or
                "REWARDCASH SUMMARY" in s or "REWARDCASH EARNED" in s or
                "REWARDCASH EARNINGS" in s or
                s.startswith("For important information") or
                s.startswith("Minimum payment summary") or
                s.startswith("SELECTED CATEGORY")):
            # Don't break — credit card statements have multiple pages; just skip these lines
            # The next transaction date or PREVIOUS BALANCE on a continuation page is what matters
            if pending is not None:
                sub.transactions.append(pending)
                pending = None
            continue

        # FX rate continuation line — attach to most recent transaction
        fxm = RE_CC_FX_RATE.search(s)
        if fxm and pending is not None:
            pending.fx_rate = parse_amount(fxm.group(1))
            sub.transactions.append(pending)
            pending = None
            continue

        # Transaction line
        m = RE_CC_TXN.match(s)
        if m:
            # Commit any previous pending (no FX rate followed)
            if pending is not None:
                sub.transactions.append(pending)
                pending = None
            post_d = _cc_short_to_date(m.group("post"), end_date)
            trans_d = _cc_short_to_date(m.group("trans"), end_date)
            rest = m.group("rest")
            amt = parse_amount(m.group("amount"))
            is_credit = bool(m.group("cr"))
            # FX inline currency+amount within the rest text
            fx_amt: Optional[float] = None
            fx_ccy = ""
            fxi = RE_CC_FX_INLINE.search(rest)
            if fxi:
                fx_ccy = fxi.group(1)
                fx_amt = parse_amount(fxi.group(2))
                # strip the FX bit from description
                rest = (rest[:fxi.start()] + rest[fxi.end():]).strip()
            desc = re.sub(r"\s{2,}", " | ", rest).strip()
            # Update running balance
            if running_balance is not None and amt is not None:
                running_balance = running_balance - amt if is_credit else running_balance + amt
            txn = Transaction(
                date_iso=trans_d.isoformat(),
                post_date_iso=post_d.isoformat(),
                description=desc or "(no description)",
                currency="HKD",
                deposit=amt if is_credit else None,
                withdrawal=amt if not is_credit else None,
                balance=running_balance,
                sub_account=sub.name,
                fx_amount=fx_amt,
                fx_currency=fx_ccy,
                fx_rate=None,  # may be set by next line
            )
            # If FX inline was found, wait for the FX rate line to commit
            if fx_amt is not None:
                pending = txn
            else:
                sub.transactions.append(txn)
            continue

        # Continuation lines that aren't FX-rate (rare — supplemental description)
        if pending is not None and not s.startswith("PREVIOUS BALANCE"):
            pending.description = (pending.description + " | " + s).strip(" |")

    # Anything left pending → commit
    if pending is not None:
        sub.transactions.append(pending)

    sub.closing_balance = running_balance
    stmt.sub_accounts.append(sub)
    return stmt


# ---------- dispatch ----------

def parse_pdf(path: Path) -> Statement:
    text = pdf_to_text(path)
    fmt = detect_format(text)
    if fmt == "credit-card":
        return _parse_cc(path, text)
    return _parse_bank(path, text)


# ---------- reporting / output ----------

def find_pdfs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")


def report(stmts: list[Statement], audit_from: Optional[str], audit_to: Optional[str]) -> None:
    print(f"\nParsed {len(stmts)} statement PDF(s).\n")
    print(f"{'Period':<8}  {'End date':<11}  {'Format':<12}  {'Account':<22}  {'File':<40}  Txns")
    print("-" * 110)
    for s in sorted(stmts, key=lambda x: (x.format, x.end_date)):
        n = sum(len(sa.transactions) for sa in s.sub_accounts)
        print(f"{s.month_key:<8}  {s.end_date.isoformat():<11}  {s.format:<12}  "
              f"{s.account_number:<22}  {s.path.name:<40}  {n}")
    by_key: dict[tuple[str, str], list[Statement]] = {}
    for s in stmts:
        by_key.setdefault((s.format, s.month_key), []).append(s)
    dupes = {k: v for k, v in by_key.items() if len(v) > 1}
    if dupes:
        print("\nDUPLICATE MONTHS DETECTED:")
        for (fmt, m), group in dupes.items():
            print(f"  {fmt} {m}: {len(group)} files")
            for s in group:
                print(f"     - {s.path.name}  md5={s.md5}")
            md5s = {s.md5 for s in group}
            print("     -> identical content" if len(md5s) == 1 else "     -> different content; review")
    else:
        print("\nNo duplicate months found.")
    if audit_from and audit_to:
        expected = month_range(audit_from, audit_to)
        by_fmt: dict[str, set[str]] = {}
        for s in stmts:
            by_fmt.setdefault(s.format, set()).add(s.month_key)
        for fmt, present in by_fmt.items():
            missing = [m for m in expected if m not in present]
            print(f"\n{fmt}: {len(expected) - len(missing)}/{len(expected)} months over {audit_from} → {audit_to}")
            for m in missing:
                print(f"  MISSING: {m}")


def write_csv(stmts: list[Statement], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "statement_month", "statement_end_date", "account_number",
            "sub_account", "currency", "txn_date", "post_date", "description",
            "deposit", "withdrawal", "balance",
            "fx_amount", "fx_currency", "fx_rate", "source_file",
        ])
        for s in sorted(stmts, key=lambda x: x.end_date):
            for sa in s.sub_accounts:
                for t in sa.transactions:
                    w.writerow([
                        s.month_key, s.end_date.isoformat(), s.account_number,
                        sa.name, t.currency, t.date_iso, t.post_date_iso, t.description,
                        f"{t.deposit:.2f}" if t.deposit is not None else "",
                        f"{t.withdrawal:.2f}" if t.withdrawal is not None else "",
                        f"{t.balance:.2f}" if t.balance is not None else "",
                        f"{t.fx_amount:.2f}" if t.fx_amount is not None else "",
                        t.fx_currency,
                        f"{t.fx_rate}" if t.fx_rate is not None else "",
                        s.path.name,
                    ])
    print(f"\nTransactions written to {out}")


def rename_files(stmts: list[Statement], style: str = "historical",
                 bank_prefix: str = "BANK", cc_prefix: str = "CC",
                 dry_run: bool = False) -> None:
    """Rename PDFs to a canonical name.

    style="historical" (default):
      bank:        <bank_prefix>_YYYYMM.pdf       (e.g. BANK_202604.pdf)
      credit-card: <cc_prefix>_YYYYMM.pdf         (e.g. CC_202604.pdf)

    style="descriptive" (sortable, format-tagged):
      bank:        YYYY-MM-HSBC-<acct-short>.pdf  (e.g. 2026-04-HSBC-082814.pdf)
      credit-card: YYYY-MM-Credit-card.pdf        (e.g. 2026-04-Credit-card.pdf)
    """
    print(f"\nRenaming files (style={style}):")
    used: set[str] = set()
    for s in sorted(stmts, key=lambda x: x.end_date):
        yyyymm = f"{s.end_date.year:04d}{s.end_date.month:02d}"
        if style == "historical":
            base = f"{bank_prefix}_{yyyymm}" if s.format == "bank" else f"{cc_prefix}_{yyyymm}"
        else:  # descriptive
            if s.format == "bank":
                acct_short = s.account_number.split("-")[1] if "-" in s.account_number else s.account_number
                base = f"{s.month_key}-HSBC-{acct_short}"
            else:
                base = f"{s.month_key}-Credit-card"
        candidate = f"{base}.pdf"
        n = 2
        while candidate in used or (s.path.with_name(candidate).exists() and s.path.with_name(candidate) != s.path):
            candidate = f"{base}_v{n}.pdf"
            n += 1
        used.add(candidate)
        target = s.path.with_name(candidate)
        if target == s.path:
            print(f"  {s.path.name:<40} -> already named correctly")
            continue
        print(f"  {s.path.name:<40} -> {candidate}")
        if not dry_run:
            s.path.rename(target)
            s.path = target


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", nargs="?", default=None,
                    help="Folder containing the PDFs (default: folder of this script).")
    ap.add_argument("--rename", action="store_true", help="Rename PDFs to canonical names")
    ap.add_argument("--style", choices=("historical", "descriptive"), default="historical",
                    help="Naming style for --rename (default: historical = <prefix>_YYYYMM.pdf)")
    ap.add_argument("--bank-prefix", default="BANK",
                    help="Filename prefix for bank statements in historical style (default: BANK)")
    ap.add_argument("--cc-prefix", default="CC",
                    help="Filename prefix for credit card statements in historical style (default: CC)")
    ap.add_argument("--dry-run", action="store_true", help="With --rename: only print proposed renames.")
    ap.add_argument("--csv", metavar="FILE", help="Write consolidated transactions to FILE.csv")
    ap.add_argument("--audit-from", help="Audit window start YYYY-MM (omit to skip the missing-month report).")
    ap.add_argument("--audit-to", help="Audit window end YYYY-MM (omit to skip the missing-month report).")
    args = ap.parse_args(argv)
    folder = Path(args.folder).resolve() if args.folder else Path(__file__).resolve().parent
    if not folder.is_dir():
        sys.stderr.write(f"ERROR: not a directory: {folder}\n")
        return 1
    pdfs = find_pdfs(folder)
    if not pdfs:
        # Allow nested layout: walk one level for "scan all years"
        pdfs = sorted(folder.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {folder}.")
        return 0
    stmts: list[Statement] = []
    for p in pdfs:
        try:
            stmts.append(parse_pdf(p))
        except Exception as e:
            sys.stderr.write(f"WARN: failed to parse {p.name}: {e}\n")
    report(stmts, args.audit_from, args.audit_to)
    if args.csv:
        write_csv(stmts, Path(args.csv))
    if args.rename:
        rename_files(stmts, style=args.style, bank_prefix=args.bank_prefix,
                     cc_prefix=args.cc_prefix, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
