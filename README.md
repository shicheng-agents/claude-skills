# claude-skills

A reusable set of Claude Code skills for running the back office of a small multi-currency company on **ERPNext + Google Drive + Gmail + a rclone toolbox container**. Extracted from a real Hong Kong trading-company setup, genericized so other operators can adapt without leaking client data.

Six skills, one per slash command:

| Skill | Trigger | What it does |
|---|---|---|
| [`email`](skills/email/SKILL.md) | `/email` | Secretary triage of two Gmail inboxes (accounting + director). Read + label only, no drafts/no send. Runs on Sonnet. |
| [`employers-return`](skills/employers-return/SKILL.md) | `/employers-return` | Annual HK IRD Employer's Return (BIR56A + IR56B) filing runbook via eTAX. |
| [`audit-reconciliation`](skills/audit-reconciliation/SKILL.md) | `/audit-reconciliation` | Year-end close: mirror external auditor's adjusting JEs into ERPNext, seed FX rates from bank statements, submit PCV. |
| [`bookkeeper`](skills/bookkeeper/SKILL.md) | `/bookkeeper` | In-year ERPNext bookkeeping — JEs, GL reconciliation, FCY revaluation, FX gotchas. |
| [`ar-ap`](skills/ar-ap/SKILL.md) | `/ar-ap` | AR aging + AP workflow + Payment Reconciliation with the four classic FX-residual traps. |
| [`archivist`](skills/archivist/SKILL.md) | `/archivist` | Google Drive search, statement parsing, document retrieval — all via a dedicated toolbox container. |

## Installation

These are [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills). The harness auto-loads skills it finds in `~/.claude/skills/` or in your project's `/config/claude/skills/`. Either clone this repo into one of those, or symlink individual skills:

```bash
git clone https://github.com/shicheng-agents/claude-skills.git ~/.claude/skills-source
ln -s ~/.claude/skills-source/skills/bookkeeper ~/.claude/skills/bookkeeper
# repeat for the skills you want
```

## Placeholder convention

These skills carry no client-specific names. When adapting, do a project-wide search-and-replace for the placeholders below. Some are referenced verbatim (in code patterns); others document a slot you'll fill in your own context.

| Placeholder | Replace with | Example |
|---|---|---|
| `{COMPANY_NAME}` | Full legal company name as it appears in ERPNext | `"Acme Trading Limited"` |
| `{ABBR}` | ERPNext company abbreviation (used in chart-of-accounts suffixes like `Cash - SC`) | `AT` |
| `{TOOLBOX}` | Name of your rclone/gog toolbox container | `acme-toolbox` |
| `{GDRIVE_REMOTE}` | rclone remote name for the company Google Drive | `acme-gdrive` |
| `{DROPBOX_REMOTE}` | rclone remote for legacy Dropbox (if applicable) | `acme-dropbox` |
| `{ACCOUNTING_EMAIL}` | Accounting / admin Gmail address | `admin@acme.example` |
| `{DIRECTOR_EMAIL}` | Director / owner Gmail address | `director@acme.example` |
| `<YYYY>` / `<YYYY-MM-DD>` etc. | Date slots for the period at hand | `2025` / `2025-12-31` |
| `<...>` placeholders inside code | Inline values you'd fill at use time | `<supplier name>`, `<JE-name>` |

## Helper scripts

Three companion scripts ship in [`scripts/`](scripts/) — referenced from the skills above.

| Script | Purpose |
|---|---|
| [`parse_statements.py`](scripts/parse_statements.py) | Parse HSBC bank + credit-card statement PDFs into a unified CSV (`txn_date, description, deposit, withdrawal, balance, fx_*`). Auto-detects format, handles FX continuation lines, can rename PDFs to a canonical `<PREFIX>_YYYYMM.pdf` layout. Requires `pdftotext` (poppler). |
| [`extract_fx_rates.sh`](scripts/extract_fx_rates.sh) | Pull month-end USD/CNY/EUR-to-HKD rates from monthly HSBC bank statements via rclone + pdftotext + awk, emits TSV. Env-configurable (`REMOTE`, `PREFIX`, `YEARS`). |
| [`export_audit_reports.py`](scripts/export_audit_reports.py) | Run inside the frappe backend container to export Trial Balance / BS / P&L / AR / GL as CSV with currency precision applied at source (fixes GL running-balance float drift). |

The bank-statement parsing is HSBC-specific (Business Direct + World Business MC formats). Other banks would need their own format detector + parsing functions, but the dataclass shape, CSV output schema, and rename machinery transfer.

## Assumed setup

The skills assume a topology like this:

- **ERPNext** at v16.x (frappe 16.x + erpnext 16.x), default site, accessed via `docker exec <backend-container> bench --site <site> ...`.
- **Two Docker containers** on the same host: the Claude Code agent container, and a separate "toolbox" container that handles all egress-heavy work (rclone, gog/Google APIs) on a direct uplink. This separation matters when the agent container is VPN-routed and the toolbox isn't — see `skills/archivist/SKILL.md` for the egress rule.
- **rclone** with at least one Google Drive remote (`{GDRIVE_REMOTE}:`), optionally a Dropbox remote (`{DROPBOX_REMOTE}:`).
- **`gog`** ([steipete/openclaw `gogcli`](https://github.com/steipete/openclaw)) or equivalent for Gmail / Drive / Calendar API access with the two accounts authorized.
- **HKD reporting**, multi-currency books (HKD/USD/EUR/CNY in examples). The narrative is HK-specific in places (IRD, Companies Ordinance, HKAS 8, HKFRS for Private Entities); other jurisdictions can adapt the same patterns.

If your setup differs (different ERP, single-currency, no toolbox split), most skills still carry value — the JE patterns, Payment Reconciliation procedure, FX-revaluation gotchas, and the multi-currency audit-mirroring workflow are general to ERPNext.

## Why publish this

The Shi Cheng setup that these were extracted from is small (one-director HK trading company, multi-currency, wind-down mode, no warehouse, no staff) but the patterns it exercises — ERPNext idempotency traps, FCY revaluation gotchas, year-end auditor reconciliation, Payment Reconciliation FX residuals, ACB-chain backfill — are common enough that hardening them in public skills seems worth more than keeping them private. Issues / PRs welcome.

## License

MIT. See `LICENSE` (to be added).
