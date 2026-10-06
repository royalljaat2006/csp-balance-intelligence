# API catalog

Every endpoint the service exposes. Live, machine-readable version: `/api/v1/docs` (Swagger UI)
and `/api/v1/openapi.json`. Auth for all business endpoints is `X-API-Key` (docs/API-CONSUMERS.md).
Errors follow docs/API-ERRORS.md. All timestamps IST (PRD §13).

| Endpoint | Method | Version | Purpose | Scope | Consumer | Status |
|---|---|---|---|---|---|---|
| `/api/v1/csps` | GET | v1 | List CSPs' monthly summary (filter by slab/trend) | `csp:read` | — | active |
| `/api/v1/csps/{csp_code}` | GET | v1 | One CSP's identity + current-month summary | `csp:read` | — | active |
| `/api/v1/csps/{csp_code}/balance` | GET | v1 | Most recent daily average balance | `balance:read` | — | active |
| `/api/v1/csps/{csp_code}/balances/history` | GET | v1 | Daily balance time series | `balance:read` | — | active |
| `/api/v1/csps/{csp_code}/incentive` | GET | v1 | Rule #19 slab / rate / gaps for a month | `incentive:read` | — | active |
| `/api/v1/csps/{csp_code}/projection` | GET | v1 | Month-end MAB projection + trend | `projection:read` | — | active |
| `/api/v1/csps/{csp_code}/transactions` | GET | v1 | ONUS-family transactions for a CSP | `transaction:read` | — | active |
| `/api/v1/csps/{csp_code}/comparison` | GET | v1 | One CSP's balance/activity/slab comparison across two dates | `comparison:read` | — | active |
| `/api/v1/csps/{csp_code}/trend` | GET | v1 | One CSP's growth/decline streaks + reversal | `comparison:read` | — | active |
| `/api/v1/csps/{csp_code}/mtd-readiness` | GET | v1 | One CSP's MTD-so-far vs previous month's MTD-so-far | `comparison:read` | — | active |
| `/api/v1/comparisons` | GET | v1 | Every CSP's comparison for a date pair — filter/sort/paginate | `comparison:read` | — | active |
| `/api/v1/comparisons/top-movers` | GET | v1 | Biggest balance movers, growth or decline | `comparison:read` | — | active |
| `/api/v1/comparisons/at-risk` | GET | v1 | Explainable at-risk CSP flags | `comparison:read` | — | active |
| `/api/v1/reports/overview` | GET | v1 | Cross-CSP slab distribution + avg MAB | `report:read` | — | active |
| `/health` | GET | — | Liveness + ingest staleness (infra, no auth) | none | Docker/Nginx healthcheck | active |

"Consumer" is empty because no external consumer has been registered yet — see
docs/API-CONSUMERS.md. Fill it in as consumers onboard.

## Detail

### GET /api/v1/csps
- **Response:** `ListEnvelope[CspSummaryOut]` — `csp_code, name, mtd_mab, projected_mab, slab, trend_flag, is_eligible` per row; `pagination.total` is the count *after* filters.
- **Params:** `month` (YYYY-MM, default current), `slab` (NIL/S1–S4), `trend` (improving/stable/declining), `limit` (default 50), `offset`.
- **Source data:** `csp_monthlysummary` (dbt mart, PRD §10).
- **Business rule:** Rule #19 slab assignment, MTD MAB (PRD §5). *Computed upstream by the dbt layer — this endpoint only reads.*
- **Owner:** biswajit.chacko@gmail.com · **Introduced:** v1 · **Deprecation:** none

### GET /api/v1/csps/{csp_code}
- **Response:** `DataEnvelope[CspDetailOut]` — identity fields + `summary` (same shape as a list row, or `null` if no data for the month).
- **Params:** `month` (optional).
- **Errors:** 404 if the CSP code is unknown.
- **Source data:** `csp_csp` + `csp_monthlysummary`.

### GET /api/v1/csps/{csp_code}/balance
- **Response:** `DataEnvelope[CurrentBalanceOut]` — `date, daily_avg_balance, source`.
- **Errors:** 404 if the CSP is unknown *or* has no balance rows yet.
- **Source data:** latest `csp_dailybalance` row for the CSP (CALLING SHEET, PRD §7.1).

### GET /api/v1/csps/{csp_code}/balances/history
- **Response:** `ListEnvelope[DailyBalanceOut]`, ascending by date.
- **Params:** `date_from`, `date_to` (YYYY-MM-DD), `limit` (default 100), `offset`.

### GET /api/v1/csps/{csp_code}/incentive
- **Response:** `DataEnvelope[IncentiveOut]` — `mtd_mab, slab, incentive_rate_pa, projected_incentive_annual, gap_to_min, gap_to_next_slab`.
- **Errors:** 404 if no monthly summary exists for that CSP/month.
- **Business rule:** Rule #19 (PRD §5.1). `projected_incentive_annual` is null until `account_count` is known (PRD Q2).

### GET /api/v1/csps/{csp_code}/projection
- **Response:** `DataEnvelope[ProjectionOut]` — `days_in_month, days_with_data, projected_mab, projected_slab, trend_7d_pct, trend_flag, mom_change_abs, mom_change_pct`.
- **Business rule:** PRD FR4 projection + trend (`csp/projection.py`) + dbt's `monthly_summary` mart. Fields are populated once `dbt run` + `manage.py sync_monthly_summary` have run for the month.

### GET /api/v1/csps/{csp_code}/transactions
- **Response:** `ListEnvelope[TransactionOut]`, newest first.
- **Params:** `date_from`, `date_to`, `limit` (default 50), `offset`.
- **Note:** Populated by `ingest_transactions` (bulk upsert, idempotent) — returns real rows once transaction files have been ingested for that CSP.

### GET /api/v1/csps/{csp_code}/comparison
- **Response:** `DataEnvelope[CspComparisonOut]` — balance/txn_count/txn_amount (each a `ComparisonMetricOut`: current/previous value, abs/pct change, trend, movement), `slab_movement`, `mtd_avg_so_far`, `gap_to_min`, `gap_to_next_slab`, `current_data_status`, `comparison_data_status`.
- **Params:** `mode` (`overall`/`onus`, default `overall`), `current_date` (default today), `comparison_type` (`yesterday`/`same_date_previous_month`/`custom`, default `yesterday`), `comparison_date` (exact date — wins over `comparison_type` if both given), `fallback` (`last_day_of_month`/`none`, for `same_date_previous_month` when the target month is shorter).
- **Errors:** 404 if the CSP code is unknown.
- **Source data:** `csp/comparison.py`'s `compare_csp_metrics()` — `csp_dailybalance`, `csp_dailycspsnapshot`, the dbt `daily_activity` mart. Never a second comparison engine — the CSP Daily Comparison dashboard tab reads the exact same function.
- **Business rule:** trend is never DECLINE for a missing reading — `NO_DATA` is a distinct state (PRD-extension "missing data must never be classified as decline").
- **Owner:** biswajit.chacko@gmail.com · **Introduced:** v1 (2026-09-18) · **Deprecation:** none

### GET /api/v1/csps/{csp_code}/trend
- **Response:** `DataEnvelope[TrendIntelligenceOut]` — `avg_7d, avg_30d, consecutive_growth_days, consecutive_decline_days, last_growth_date, last_decline_date, trend_reversal`.
- **Params:** `as_of_date` (default today), `lookback_days` (default 45 — bounds how far back streaks are walked).
- **Source data:** `csp/comparison.py`'s `get_trend_intelligence()`, walking real `csp_dailybalance` history.

### GET /api/v1/csps/{csp_code}/mtd-readiness
- **Response:** `DataEnvelope[MtdReadinessOut]` — `current_mtd, current_days, previous_mtd, comparison_date, comparison`.
- **Params:** `as_of_date` (default today).
- **Source data:** `csp/comparison.py`'s `get_mtd_readiness()` — the same-date-previous-month comparison, at MTD grain.

### GET /api/v1/comparisons
- **Response:** `ListEnvelope[CspComparisonOut]`.
- **Params:** same `mode`/date params as `.../comparison` above, plus `trend`, `movement`, `slab`, `search` (filters) and `sort_by` (`pct_change`/`abs_change`/`csp_code`/`csp_name`/`mtd_avg_so_far`), `sort_dir` (`asc`/`desc`), `limit`/`offset`.
- **Source data:** `bulk_compare_csps()` (one batched query per data source across every CSP, not one per CSP) + `filter_and_sort_comparisons()` — the exact function the dashboard tab's table also calls.

### GET /api/v1/comparisons/top-movers
- **Response:** `ListEnvelope[CspComparisonOut]`.
- **Params:** same date/mode params, plus `by` (`pct`/`abs`), `direction` (`growth`/`decline`), `limit` (default 10).

### GET /api/v1/comparisons/at-risk
- **Response:** `ListEnvelope[AtRiskFlagOut]` — `csp_code, csp_name, reasons` (a list of human-readable strings, never an opaque score).
- **Params:** same date/mode params, plus `near_threshold` (rupees; default 200 — how close to the eligibility minimum counts as "at risk").
- **Business rule:** flags a sharp decline, a slab downgrade, or `mtd_avg_so_far` within `near_threshold` rupees *above* `rules.MIN_BALANCE` — never `gap_to_min` (which is only nonzero once a CSP has *already* fallen below the minimum).

### GET /api/v1/reports/overview
- **Response:** `DataEnvelope[OverviewOut]` — `csp_total, csp_with_data, slab_distribution{NIL,S1..S4}, in_nil_count, compliant_count, avg_mab`.
- **Params:** `month`.
- **Performance:** iterates every `csp_monthlysummary` row for the month (~550). Fine today; switch to a DB aggregate if it grows.

## Performance characteristics (all endpoints)

Every read is served from precomputed tables (`csp_monthlysummary`, `csp_dailybalance`) —
no calculation happens at request time. p95 target < 300 ms (PRD §13). No rate limiting exists
yet (no 429 path); add django-ninja throttling per consumer if a consumer misbehaves.
