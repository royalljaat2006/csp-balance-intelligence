# dbt — calculation layer

Builds the `daily_activity` and `monthly_summary` marts described in PRD §8 FR4 / §10, on top of
the raw tables Django ingests into (`csp_csp`, `csp_dailybalance`, `csp_transaction` — owned by the
`csp` Django app, see `app/csp/models.py`).

## Status: implemented and verified

- `models/staging/` — `stg_csp`, `stg_daily_balance`, `stg_transaction` (thin pass-throughs).
- `models/marts/daily_activity.sql` — per-CSP per-day ONUS activity rollup.
- `models/marts/monthly_summary.sql` — MTD MAB, slab, gap-to-min/next-slab, MoM, 7-day trend.
  Rule #19 thresholds live in `macros/rule_19.sql` (kept in sync with the canonical Python copy,
  `app/csp/rules.py` — see comments in both files).
- `tests/` — 3 singular tests (uniqueness per csp+month/day, `gap_to_min >= 0`) plus generic
  `not_null`/`accepted_values` tests in `models/marts/schema.yml`. **12/12 passing** against a real
  Postgres 17 instance loaded with the actual transaction sample (`dbt run` + `dbt test`).

**Not part of dbt** (by design — see `app/csp/projection.py`'s docstring): `projected_mab`,
`projected_slab`, `projected_incentive_annual`, and the human-readable `trend_flag` are a
forward-looking estimate, not a deterministic aggregate, so they're computed in Python by
`manage.py sync_monthly_summary` (in the `csp` Django app) after `dbt run`, and written into the
Django-owned `csp_monthlysummary` table — the one the API actually reads. dbt's `monthly_summary`
mart lives in the same Postgres schema under its own name and is *not* the same table.

**Still pending:** real CALLING SHEET data. `stg_daily_balance` and everything downstream of it
already work correctly — verified with `manage.py seed_demo_balances` (dev-only synthetic data,
see that command's docstring) — but `csp_dailybalance` has no real rows until PRD §16 Q1/Q8 (sheet
access) is resolved and `ingest_calling_sheet` is implemented.

## Running it

Requires a live Postgres with the Django migrations already applied (`csp_csp`, `csp_dailybalance`,
`csp_transaction` tables must exist) and `DATABASE_HOST/PORT/USER/PASSWORD/NAME` set in the
environment `dbt` runs in.

```bash
cd dbt
DBT_PROFILES_DIR=. uv run dbt debug   # verify the connection
DBT_PROFILES_DIR=. uv run dbt run
DBT_PROFILES_DIR=. uv run dbt test
```

In production this runs as a cron step in the `worker` container, right after
`ingest_calling_sheet`/`ingest_transactions`, followed by `manage.py sync_monthly_summary`
(PRD §14; see docs/DEPLOYMENT.md "Scheduled jobs").
