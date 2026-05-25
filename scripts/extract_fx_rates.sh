#!/bin/sh
# Extracts HSBC monthly exchange rates from bank statement PDFs and writes
# TSV: date<TAB>from_currency<TAB>to_currency<TAB>rate to stdout.
#
# Env vars (with defaults):
#   REMOTE   rclone remote + path to the year-keyed bank-statement folder
#            default: gdrive:Accounting/Statements/Bank
#   PREFIX   filename prefix matching your bank-statement naming convention,
#            e.g. PREFIX=BANK matches files like BANK_202512.pdf
#            default: BANK
#   YEARS    space-separated list of years to scan
#            default: 2022 2023 2024 2025 2026
#
# Usage examples:
#   ./extract_fx_rates.sh
#   REMOTE="acme-gdrive:Accounting/Statements/Bank" PREFIX=BK YEARS="2024 2025" ./extract_fx_rates.sh
#
# Assumes HSBC's "Exchange Rate" block in the monthly statement, which lists
# USD/CNY/EUR mid-rates to HKD as of the statement-end date.
set -eu
REMOTE="${REMOTE:-gdrive:Accounting/Statements/Bank}"
PREFIX="${PREFIX:-BANK}"
YEARS="${YEARS:-2022 2023 2024 2025 2026}"

# month end days; Feb fixed at 28 (HSBC statement dates may differ but 28 is safe lower bound)
last_day() {
  case "$1" in
    01|03|05|07|08|10|12) echo 31 ;;
    04|06|09|11)          echo 30 ;;
    02)                   echo 28 ;;
  esac
}

for year in $YEARS; do
  for f in $(rclone lsf "$REMOTE/$year/" --include "${PREFIX}_*.pdf" --files-only 2>/dev/null | sort); do
    mo=$(echo "$f" | sed -nE "s/${PREFIX}_([0-9]{4})([0-9]{2})\\.pdf/\\2/p")
    [ -z "$mo" ] && continue
    day=$(last_day "$mo")
    date="${year}-${mo}-${day}"
    line=$(rclone cat "$REMOTE/$year/$f" 2>/dev/null \
      | pdftotext -layout - - 2>/dev/null \
      | awk '/^[[:space:]]*Exchange Rate[[:space:]]*$/{getline l; print l; exit}')
    [ -z "$line" ] && continue
    # parse USD/CNY/EUR rates from line (order can vary)
    echo "$line" | tr -s ' ' | tr ' ' '\n' \
      | awk -v d="$date" '
        /^USD$/   { getline r; printf "%s\tUSD\tHKD\t%s\n", d, r; next }
        /^CNY$/   { getline r; printf "%s\tCNY\tHKD\t%s\n", d, r; next }
        /^EUR$/   { getline r; printf "%s\tEUR\tHKD\t%s\n", d, r; next }
      '
  done
done
