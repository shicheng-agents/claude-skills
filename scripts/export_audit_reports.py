#!/usr/bin/env python3
"""Export ERPNext audit-pack reports (TB, BS, P&L, AR, GL) as CSV with
currency-typed columns rounded to the System-Settings currency precision.

Run inside the frappe backend container, e.g.:

    docker exec <frappe-backend-container> bash -c \\
        "cd /home/frappe/frappe-bench/sites && ../env/bin/python \\
         /path/to/this/script.py --company '<Your Company Name>' \\
         --fiscal-year 2025 --out /tmp/audit_fy2025"

Numeric columns whose fieldtype is Currency, Float, Int, or Percent are
rounded via frappe.utils.flt() — same call ERPNext's UI export uses —
so the raw float drift that otherwise leaks into the CSV as 1e-13 noise
is suppressed at source.

Presentation currency defaults to HKD; override with --currency.
"""
import argparse, csv, json, os, sys

import frappe
from frappe.desk.query_report import run
from frappe.utils import flt

NUMERIC_FIELDTYPES = {"Currency", "Float", "Int", "Percent"}


def column_meta(col):
    """Return (label, fieldname, fieldtype, precision) for a query-report column."""
    if isinstance(col, dict):
        return (
            col.get("label") or col.get("fieldname") or "",
            col.get("fieldname") or col.get("label"),
            col.get("fieldtype") or "Data",
            col.get("precision"),
        )
    # String form e.g. "Label:Currency:120" or "Label::120"
    parts = col.split(":")
    label = parts[0] if parts else ""
    fieldtype = parts[1] if len(parts) > 1 and parts[1] else "Data"
    return (label, label, fieldtype, None)


def format_cell(value, fieldtype, precision):
    if value in (None, ""):
        return ""
    if fieldtype in NUMERIC_FIELDTYPES:
        try:
            return f"{flt(value, precision or get_currency_precision()):.{precision or get_currency_precision()}f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


_currency_precision_cache = None
def get_currency_precision():
    global _currency_precision_cache
    if _currency_precision_cache is None:
        _currency_precision_cache = (
            frappe.db.get_default("currency_precision")
            or frappe.db.get_single_value("System Settings", "currency_precision")
            or 2
        )
        _currency_precision_cache = int(_currency_precision_cache)
    return _currency_precision_cache


def write_report(report_name, filters, out_path):
    res = run(report_name, json.dumps(filters))
    columns = res.get("columns", [])
    rows = res.get("result", [])

    meta = [column_meta(c) for c in columns]
    headers = [m[0] for m in meta]
    fieldnames = [m[1] for m in meta]

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            if isinstance(r, dict):
                vals = [r.get(fn, "") for fn in fieldnames]
            elif isinstance(r, list):
                vals = list(r) + [""] * (len(meta) - len(r))
            else:
                vals = [r] + [""] * (len(meta) - 1)
            w.writerow([format_cell(v, m[2], m[3]) for v, m in zip(vals, meta)])
    print(f"  wrote {out_path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="frontend")
    ap.add_argument("--company", required=True)
    ap.add_argument("--fiscal-year", required=True)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--currency", default="HKD",
                    help="presentation currency for the reports (default: HKD)")
    args = ap.parse_args()

    frappe.init(site=args.site)
    frappe.connect()

    os.makedirs(args.out, exist_ok=True)
    fy = args.fiscal_year
    from_date = f"{fy}-01-01"
    to_date = f"{fy}-12-31"

    tb_filters = {
        "company": args.company,
        "from_date": from_date, "to_date": to_date,
        "fiscal_year": fy,
        "filter_based_on": "Date Range",
        "periodicity": "Yearly",
        "presentation_currency": args.currency,
        "with_period_closing_entry_for_opening": 1,
        "show_net_values_in_party_account": 0,
    }
    fs_filters = {
        "company": args.company,
        "filter_based_on": "Fiscal Year",
        "from_fiscal_year": fy, "to_fiscal_year": fy,
        "period_start_date": from_date, "period_end_date": to_date,
        "periodicity": "Yearly",
        "presentation_currency": args.currency,
        "accumulated_values": 1,
    }
    pl_filters = {**fs_filters, "accumulated_values": 0}
    ar_filters = {
        "company": args.company,
        "report_date": to_date,
        "ageing_based_on": "Posting Date",
        "range1": 30, "range2": 60, "range3": 90, "range4": 120,
        "show_future_payments": 0, "based_on_payment_terms": 0,
    }
    gl_filters = {
        "company": args.company,
        "from_date": from_date, "to_date": to_date,
        "include_dimensions": 1,
        "show_opening_entries": 0,
        "include_default_book_entries": 1,
        "categorize_by": "Categorize by Voucher (Consolidated)",
        "group_by": "Group by Voucher (Consolidated)",
        "presentation_currency": args.currency,
    }

    jobs = [
        ("Trial Balance",              tb_filters, f"Trial_Balance_FY{fy}.csv"),
        ("Balance Sheet",              fs_filters, f"Balance_Sheet_FY{fy}.csv"),
        ("Profit and Loss Statement",  pl_filters, f"Profit_and_Loss_Statement_FY{fy}.csv"),
        ("Accounts Receivable",        ar_filters, f"Accounts_Receivable_FY{fy}.csv"),
        ("General Ledger",             gl_filters, f"General_Ledger_FY{fy}.csv"),
    ]
    for name, filters, fname in jobs:
        print(f"{name}...")
        write_report(name, filters, os.path.join(args.out, fname))


if __name__ == "__main__":
    main()
