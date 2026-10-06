# CSP Average Balance Tracker

A full web app for tracking the **Monthly Average Balance (MAB)** each SBI-kiosk CSP maintains
against the SBI Rule #19 incentive slabs: a staff-facing dashboard with trends, plus an
API-key-authenticated REST API for other Eko services to consume the same data.

Full product spec: [PRD.md](PRD.md). Deeper docs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/DATA-FLOW.md](docs/DATA-FLOW.md), [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md),
[docs/OPERATIONS.md](docs/OPERATIONS.md). This README is the developer-facing quick start.

## Architecture summary

```
Google Sheet (CALLING) ──▶ ingest_calling_sheet ──┐
Watched folder / Telegram ──▶ ingest_transactions ┼─▶ PostgreSQL ──▶ dbt ──▶ sync_monthly_summary
                          (poll_telegram feeds the same folder)   (calc layer)  (projection)
        (all run inside the `worker` container, on independent schedules)          │
                                                          csp/comparison.py ◀───────┤ (daily snapshots)
                                                                    │
                                                    ┌───────────────┴───────────────┐
                                                    ▼                               ▼
                                          Dashboard (session auth)          API (X-API-Key)
                                          Eko staff, browser                 other Eko services
```

- **Dashboard** (`/dashboard/`): server-rendered, staff-login-gated. Overview, per-CSP detail,
  network-wide trends, and CSP Daily Comparison (today-vs-yesterday / same-date-previous-month,
  top movers, at-risk flags) — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#two-front-doors-one-domain-layer).
- **API** (`/api/v1/`): no end-user login. Consumers authenticate with an `X-API-Key` header.
- **Admin** (`/admin/`): Django admin console for Eko ops to browse data and ingestion health.
- **Ingestion**: daily batch (CALLING SHEET, watched folder) plus near-live Telegram delivery to a
  private bot chat — see [docs/DATA-FLOW.md](docs/DATA-FLOW.md).
- **Three containers, one image**: `web` (dashboard+API+admin), `worker` (ingestion loop), `db` (Postgres).

## Project layout

```
app/
  config/settings/   base.py, development.py, production.py
  csp/               domain models, admin, rules.py (Rule #19), projection.py, services.py,
                     management commands (sync_monthly_summary, seed_demo_balances [dev only])
  api/               v1/ (one router per resource) + auth, errors, middleware, schemas,
                     ApiConsumer/ApiKey models, create_api_key/revoke_api_key commands
  dashboard/         staff-login-gated web UI — views.py, templates/, static/ (CSS/JS, no CDN libs)
  ingestion/         management commands + validation/file-safety/transaction-upsert helpers
  common/            /health
dbt/                 staging + marts (daily_activity, monthly_summary) + macros + tests — see dbt/README.md
tests/               integration/ (DB+HTTP), unit/ (pure functions)
scripts/             entrypoint.sh (web/worker role switch)
data/                local-dev incoming/processed/failed skeleton (gitignored contents)
docs/                ARCHITECTURE, DATA-FLOW, DEPLOYMENT, OPERATIONS, API-*
```

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker + Docker Compose (for local Postgres, and for the production image)
- A Google service account with read access to the CALLING SHEET, key stored **outside this repo**
  (see docs/DEPLOYMENT.md "Secrets")

## Local setup

```bash
uv sync --group dbt          # installs app deps + dbt-core/dbt-postgres (dev tools included by default)
cp .env.example .env         # fill in real values; .env is gitignored
```

For local development without Docker, `DATABASE_URL` can be left unset — Django falls back to a
local `dev.sqlite3` file. For anything resembling production behaviour, run Postgres via Compose
(see below) and point `DATABASE_URL` at it.

## Environment variables

See [.env.example](.env.example) for the full list with comments. Key ones:

| Variable | Purpose |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.development` locally, `config.settings.production` in Docker |
| `DATABASE_URL` | Postgres connection string (falls back to SQLite if unset, dev only) |
| `GOOGLE_SERVICE_ACCOUNT_FILE_HOST`, `CALLING_SHEET_ID` | CALLING SHEET ingestion (PRD §7.1) |
| `TRANSACTION_DATA_DIR_HOST` | Host folder bind-mounted as `/data` in `worker` (PRD §7.2) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram-delivered transaction sheets — blank token disables it entirely; see docs/OPERATIONS.md "Getting the Telegram chat_id" |
| *(API keys)* | Not env vars — per-consumer, DB-backed. Issue with `manage.py create_api_key` (docs/API-CONSUMERS.md) |
| `APP_PORT` | Host port — **admin-assigned**, see docs/DEPLOYMENT.md |

## Running tests

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy app/csp app/api app/ingestion app/common app/config app/dashboard
```

`tests/unit/` — pure functions (ingestion validation, file-safety), no DB.
`tests/integration/` — hits the DB and HTTP layer (health, API auth).

## Running locally

```bash
cd app
uv run python manage.py migrate
uv run python manage.py createsuperuser   # for the admin console
uv run python manage.py runserver
```

- API: http://127.0.0.1:8000/api/v1/... (send `X-API-Key: <key from create_api_key>`); Swagger UI at http://127.0.0.1:8000/api/v1/docs
  ```bash
  uv run python manage.py create_api_key local-dev --scopes csp:read,balance:read,incentive:read,projection:read,transaction:read,report:read
  ```
- Dashboard: http://127.0.0.1:8000/ (redirects to `/dashboard/`, login required)
- Admin: http://127.0.0.1:8000/admin/
- Health: http://127.0.0.1:8000/health

`collectstatic` runs automatically on every `web` container start (whitenoise serves the
dashboard's CSS/JS, no Nginx static config needed) — for `runserver` locally, Django serves static
files itself when `DEBUG=True`, so no extra step is needed there.

To exercise ingestion locally: drop a `.xlsx` file into `data/incoming/` and run
`uv run python manage.py ingest_transactions` from `app/` (or `watch_transactions --once`).

To exercise the calculation layer without CALLING SHEET access yet:

```bash
uv run python manage.py seed_demo_balances --month 2026-08   # dev-only synthetic balances
cd ../dbt && DBT_PROFILES_DIR=. uv run dbt run && uv run dbt test
cd ../app && uv run python manage.py sync_monthly_summary
```

## Docker / Compose usage

```bash
docker compose config          # validate
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:${APP_PORT}/health
```

`compose.yml` runs `web` (bound to `127.0.0.1:${APP_PORT}` only), `worker` (the ingestion loop, no
published port), and `db` (Postgres, no host port). Production deployment on the R730 is performed
by an authorized administrator; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Exposed application port

`web` listens on `8000` inside the container. The host-side port (`APP_PORT`) is assigned by the
R730 administrator from the `8200-8299` range — see docs/DEPLOYMENT.md. Not yet assigned:
`.env.example` carries `APP_PORT=CHANGE_ME` until it is.

## Health endpoint

`GET /health` — checks DB connectivity and reports whether the CALLING SHEET ingest is stale
(no successful run in the last 26h). No auth required; does not leak secrets or do expensive work.

## Database / dependencies

PostgreSQL 17 (Compose) / any Postgres the admin points `DATABASE_URL` at in production. No other
external services. Outbound HTTPS to the Google Sheets API is required for ingestion.

## Calculation layer (dbt + projection)

The `dbt/` project builds the `daily_activity` and `monthly_summary` marts from the raw tables
Django ingests into — Rule #19 slab/rate/gap math and MoM/trend are computed there. `manage.py
sync_monthly_summary` then adds the month-end projection (`app/csp/projection.py`) and writes the
combined row into `csp_monthlysummary`, which the API reads. Verified end-to-end (dbt run + dbt
test, 12/12 passing) against a real Postgres loaded with the actual transaction sample — see
`dbt/README.md` and [docs/DATA-FLOW.md](docs/DATA-FLOW.md).

## Troubleshooting

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for a fuller table. Quick ones:

- **`/health` returns `db_unreachable`**: check `DATABASE_URL` / that the `db` container is healthy.
- **API returns 401**: missing/invalid `X-API-Key` header.
- **`web` won't start with `ImproperlyConfigured`**: production settings refuse to start with a
  placeholder `DJANGO_SECRET_KEY` or empty API keys — set real values in `.env`.

## Status

**The full pipeline is implemented and verified against real data** — the live CALLING SHEET
(539 CSPs) and the real `Transaction Aug'26 (4).xlsx` (306,035 rows) — through a real Docker
`web`+`worker`+`db` stack on real PostgreSQL 17 (`pytest`/`ruff`/`mypy`/`docker compose` all
green, 312 tests passing). First live run: 539 CSPs, 483 reporting a balance (NIL 81 · S1 138 ·
S2 160 · S3 86 · S4 18), ₹375.8 Cr total balance. Rule #19 math cross-checked against the sheet's
own reference figures. See [docs/DATA-FLOW.md](docs/DATA-FLOW.md) for the stage-by-stage detail.

**2026-09-18 extension** (historical comparison engine, CSP Daily Comparison dashboard tab,
`/api/v1/comparisons*`, Telegram ingestion): implemented and unit/integration tested, manually
verified against synthetic data through a real dev server. Not yet verified against live
Telegram traffic (`TELEGRAM_CHAT_ID` still needs to be set) or against 2+ months of the real
539-CSP historical dataset (backfill not yet done) — see docs/DATA-FLOW.md's "Extension" section.
