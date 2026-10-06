# Data flow — CSP Average Balance Tracker

**Status: the full pipeline below is implemented and verified end-to-end against real data** —
the live CALLING SHEET (539 CSPs) and the real `Transaction Aug'26 (4).xlsx` (306,035 rows) —
through a real Docker `web`+`worker`+`db` stack on PostgreSQL 17. No stage is a stub anymore.

**2026-09-18 extension** (historical comparison + Telegram ingestion, see "Extension" section
below): implemented, unit/integration tested (comparison-engine math, dedup, chat-id allow-list,
worker scheduling), and manually verified end-to-end against synthetic dev data through a real
dev server (dashboard tab, Overview KPIs, Trends panel, CSP-detail panel all render and their
client-side JS was executed against the real rendered output). **Not yet** verified against a
live Telegram delivery (`TELEGRAM_CHAT_ID` still unset — no message has been sent to the bot) or
the real 539-CSP dataset over multiple real days — see docs/OPERATIONS.md.

```
Google Sheet "Calling Sheet" (worksheet         Transaction .xlsx (dropped daily)
  "Calling Sheet New") — CSP master +             watched folder: data/incoming/
  current avg balance, PRD §7.1                    PRD §7.2, Option A
        │                                             │
        ▼                                             ▼
manage.py ingest_calling_sheet             manage.py ingest_transactions
  matches columns by header text              validates, bulk-upserts, files.
  ("CSP ID", "Avg Balance", ...);             306,035 read, 220,098 upserted,
  535/539 rows read, 483 with a               0 rejected (real sample)
  parseable balance                                  │
        │                                    ┌────────┴────────┐
        │                                    ▼                 ▼
        │                          data/processed/     data/failed/
        │                          (ingested files)     (rejected, never deleted)
        │
        ▼
                    PostgreSQL — csp app tables
        csp_csp · csp_dailybalance · csp_transaction · csp_ingestlog
                                │
                                ▼
                    dbt (staging -> marts) — 12/12 tests passing
        stg_csp, stg_daily_balance, stg_transaction
                    -> daily_activity (11,146 rows on the sample file)
                    -> monthly_summary (483 rows: MTD MAB, slab, gaps, MoM, trend_7d_pct)
                                │
                                ▼
                    manage.py sync_monthly_summary
        reads the dbt mart, calls csp/projection.py (projected_mab,
        projected_slab, projected_incentive_annual, trend_flag),
        upserts into csp_monthlysummary (483 rows synced)
                                │
                                ▼
                    csp_monthlysummary (API reads this directly)
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
        Django + django-ninja API    Django admin
        GET /api/v1/reports/overview    (Eko ops browse
        GET /api/v1/csps                 raw + computed data)
        GET /api/v1/csps/{code}/...
                    │
                    ▼
        external dashboard / agent
        (X-API-Key auth)
```

## Stage-by-stage status

| Stage | File(s) | Status |
|---|---|---|
| CALLING SHEET read | `ingestion/calling_sheet_ingest.py`, `ingestion/management/commands/ingest_calling_sheet.py` | **Real** — matches the 8 required columns by header text (row 2 of "Calling Sheet New"; row 1 is merged section labels). Parses Indian-formatted currency/counts (`"₹ 34,00,000"`, `"1,781"`), extracts the first valid number from multi-number mobile cells. Verified against the live sheet: 535/539 rows read, 483 with a parseable balance, 0 crashes |
| Transaction file validation | `ingestion/xlsx_validation.py`, `ingestion/file_safety.py` | **Real** — extension/size/sheet/column checks, 7-type allow-list classification, move-to-processed/failed, unit-tested |
| Transaction file → DB | `ingestion/transaction_ingest.py`, `ingestion/management/commands/ingest_transactions.py` | **Real** — bulk-upserts allow-listed rows into `csp.models.Transaction`, keyed on `ref_number` (idempotent re-import verified). Unknown `CSP Code New` values are stub-created in `csp.models.Csp` — only **4** needed stubbing once the real CALLING SHEET data was loaded first (vs. 481 when transactions were loaded alone), confirming the two sources reconcile |
| Watched-folder loop | `ingestion/management/commands/watch_transactions.py` | **Real** — the `worker` container's main process, polls `data/incoming/` every `WATCH_INTERVAL_SECONDS` (default 300s) |
| Raw → marts (dbt) | `dbt/models/staging/*.sql`, `dbt/models/marts/{daily_activity,monthly_summary}.sql`, `dbt/macros/rule_19.sql` | **Real** — `dbt run` + `dbt test` (12 tests) pass against real Postgres loaded with the real CALLING SHEET + transaction data: 483 `monthly_summary` rows, 11,146 `daily_activity` rows |
| Month-end projection | `csp/projection.py`, `csp/rules.py` | **Real** — Rule #19 slab/rate/gap math (`csp/rules.py`, 28 unit tests) + month-end MAB extrapolation and trend flag (`csp/projection.py`, 16 unit tests). Cross-checked against the sheet's own "Amount to be deposited" reference figure — e.g. an example CSP: tracker computes ₹5,24,835, sheet shows ₹5,30,000 (same slab math, rounding-level difference; illustrative figures, not a specific real CSP's) |
| dbt mart → API table sync | `csp/management/commands/sync_monthly_summary.py` | **Real** — reads the dbt mart via raw SQL (Postgres-only: `to_char`, `LATERAL`), calls `csp/projection.py`, upserts `csp_monthlysummary`. Idempotent (verified); synced 483 real rows |
| API | `api/v1/*.py` + `csp/services.py` | **Real** — verified end-to-end against the real dataset through the full Docker stack |
| Admin | `csp/admin.py`, `api/admin.py` | **Real** — all domain models + API consumers/keys browsable/searchable/filterable |

**Real result, 2026-09-13 (first live run):** 539 CSPs, 483 reporting a balance — NIL 81 ·
S1 138 · S2 160 · S3 86 · S4 18. ₹375.8 Cr total balance across 7.61 lakh accounts.
Trend/MoM columns read "new" — they populate after a few more days of daily snapshots.

## Where each business rule lives

- Rule #19 slabs, gap-to-min/next-slab: **`app/csp/rules.py`** (canonical) — also duplicated in
  **`dbt/macros/rule_19.sql`** for the SQL side of `monthly_summary`. Keep both in sync; see the
  comments in each file.
- MTD MAB, MoM, 7-day trend: **`dbt/models/marts/monthly_summary.sql`** (deterministic aggregates).
- Month-end projection, `trend_flag`: **`app/csp/projection.py`** (a forward estimate — Python, not
  SQL — see that module's docstring for why).
- The 7-type ONUS transaction allow-list, `Success`-sheet-only scope, required columns, and
  `CSP Code New` handling: **`app/ingestion/xlsx_validation.py`** / **`app/ingestion/transaction_ingest.py`**.
- CALLING SHEET column matching, Indian-currency parsing, multi-number mobile handling:
  **`app/ingestion/calling_sheet_ingest.py`**.

All of the above encode facts already decided in **PRD.md §5, §7.1, §7.2, §8** — nothing here is a
new business rule invented during implementation.

## Extension (2026-09-18): historical comparison + Telegram ingestion

```
                 Telegram Bot API                    Google Sheet / Transaction .xlsx
              (private chat, allow-listed)                  (as above)
                        │                                       │
                        ▼                                       │
          manage.py poll_telegram                               │
       chat-id allow-list, dedup on                              │
       file_unique_id, downloads into                            │
       data/incoming/ ────────────────────────────────────────► │
                                                                  ▼
                                                    manage.py ingest_transactions
                                                    (ingestion/file_ingest.py — the
                                                     SAME orchestrator, regardless
                                                     of source="transactions"/"telegram")
                                                                  │
                                                                  ▼
                                                    csp_transaction, csp_ingestlog
                                                    (source_hash/external_ref record
                                                     which channel + which file/update)

        csp_dailybalance (CALLING SHEET, as above)
                        │
                        ▼
        manage.py sync_daily_snapshots (worker schedule, N+1-free bulk builder)
                        │
                        ▼
        csp_dailycspsnapshot (daily Rule 19 slab, MTD-so-far average)
                        │
                        ▼
        csp/comparison.py — compare_csp_metrics / bulk_compare_csps /
        get_trend_intelligence / top_movers / at_risk_csps / filter_and_sort_comparisons
                        │
        ┌───────────────┼───────────────────────────────┐
        ▼               ▼                               ▼
  /api/v1/comparisons*   dashboard: CSP Daily Comparison   dashboard: Overview KPIs,
  (comparison:read)      tab, CSP-detail panel             Trends panel
```

- **Telegram delivery** lands in the exact same `data/incoming/` folder `ingest_transactions`
  already watches, and is handed to the exact same `ingest_transaction_file()` orchestrator —
  there is only one transaction-ingestion engine regardless of delivery channel.
- **`DailyCspSnapshot`** is the one genuinely new table: `DailyBalance` and the dbt
  `daily_activity` mart already were the canonical daily balance/transaction history; only a
  *daily* Rule 19 slab classification was missing (`MonthlySummary` is month-grain).
- **Historical backfill** (loading the earlier May/June/July transaction files so 2+ months of
  real comparison history exist) is intentionally **not yet done** — the comparison engine
  correctly reports `NO_DATA` for any date before real data exists rather than fabricating a
  number; backfilling is a separate, not-yet-scoped follow-up (see docs/OPERATIONS.md).
