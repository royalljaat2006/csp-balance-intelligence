# Architecture — CSP Average Balance Tracker

> **2026-09-21 scalability/hardening update**: the diagram below predates the scalability
> foundation and production-hardening passes. Current topology (see `docs/DEPLOYMENT.md`
> "Services / containers" for the authoritative, up-to-date table): `web` is now N stateless
> replicas behind an `lb` (nginx) load balancer; the single `worker` container is split into four
> independently-scalable roles (`worker-ingestion`, `worker-calling-sheet`, `worker-telegram`,
> `worker-autopilot`), each running the same `watch_transactions` command scoped via `--jobs`; a
> `redis` service backs caching, API rate limiting, and the CSP Operations Agent's async job queue
> (consumed by a new `agent-worker` service, `manage.py run_agent_worker`); an opt-in `pgbouncer`
> service exists for connection pooling once replica count grows. `/health` split into
> `/health/live` and `/health/ready`. None of this changes the domain-service/ONUS-Overall/agent
> business logic described below — it's purely how many processes run that logic and how they're
> wired together.

## Components

```
                        ┌─────────────────────────────────────────┐
                        │              PostgreSQL 17                │
                        │  (csp_csp, csp_dailybalance,              │
                        │   csp_transaction, csp_monthlysummary,    │
                        │   csp_dailycspsnapshot,                   │
                        │   csp_ingestlog — owned by app/csp/)      │
                        └───────────────┬─────────────┬─────────────┘
                                        │             │
                     writes             │             │  reads (marts)
                                        │             │
        ┌──────────────────────────────┴───┐   ┌─────┴──────────────────┐
        │        worker container          │   │      dbt (dbt/)        │
        │  manage.py watch_transactions     │   │  staging -> marts:     │
        │  (poll loop, PRD §7.2 Option A;   │   │  daily_activity        │
        │   5 independent schedules)        │   │  (ONUS + Overall),     │
        │       │                            │   │  monthly_summary       │
        │       ▼                            │   │  (verified — 12/12    │
        │  manage.py ingest_transactions     │   │   dbt tests pass)     │
        │  manage.py ingest_calling_sheet    │   └───────────┬─────────────┘
        │  manage.py sync_daily_snapshots   │◀──────────────┘
        │  manage.py poll_telegram          │
        │  manage.py sync_monthly_summary   │
        │  (dbt mart + csp/projection.py)   │
        └──────────────┬────────────────────┘
                        │ reads
        ┌───────────────┴────────────────────────────┐
        │  /data (bind mount)         Google Sheets API │
        │   incoming/ processed/     (CALLING SHEET,     │
        │   failed/                   PRD §7.1)          │
        │  Telegram Bot API — files land in incoming/    │
        │  too (manage.py poll_telegram), same pipeline  │
        └──────────────────────────────────────────────┘

        ┌───────────────────────────────────────────────────────┐
        │                     web container                       │
        │  Django + django-ninja API (app/api/) — X-API-Key       │
        │  dashboard (app/dashboard/) — session auth, staff-only  │
        │    reads csp.services directly, same as the API does    │
        │  Django admin (app/csp/admin.py, app/api/admin.py)      │
        │  GET /health (app/common/views.py)                      │
        │  whitenoise serves dashboard's static/ (CSS/JS)         │
        └───────────────┬───────────────────────────────────────┘
                        │ 127.0.0.1:${APP_PORT} only
                        ▼
              Nginx (R730 host, outside this repo)
                        │
                     Internet
```

## App layout (`app/`)

| App | Owns | Notes |
|---|---|---|
| `csp` | The domain models (`Csp`, `DailyBalance`, `Transaction`, `MonthlySummary`, `DailyCspSnapshot`, `IngestLog`) + Django admin, `rules.py` (Rule #19, canonical), `projection.py` (month-end estimate), `comparison.py` (canonical historical-comparison engine — see below), `services.py` (query layer shared by the API **and** the dashboard), management commands `sync_monthly_summary`, `sync_daily_snapshots` + `seed_demo_balances` (dev only) | The only app with migrations besides `api` |
| `api` | `v1/` (one router module per resource: csps, balances, incentives, projections, transactions, comparisons, reports), shared `auth.py` / `errors.py` / `middleware.py` / `schemas.py`, and the `ApiConsumer`/`ApiKey` models | Owns only credential models; business data is read via `csp.services`/`csp.comparison` |
| `dashboard` | Server-rendered, staff-login-gated web UI: `views.py`, `templates/dashboard/*.html` (Overview, Trends, **CSP Daily Comparison**, CSP detail, ...), `static/dashboard/{dashboard.css,dashboard.js}` | No models, no API key involved — Django session auth only (see "Two front doors" below) |
| `ingestion` | Management commands (`ingest_calling_sheet`, `ingest_transactions`, `poll_telegram`, `watch_transactions`) + validation/file-safety/`transaction_ingest.py` (parse+upsert)/`file_ingest.py` (shared validate→parse→upsert→file orchestrator)/`telegram_client.py` (Bot API wrapper) helpers | No models — writes `csp.models` |
| `common` | Cross-cutting, currently just `/health` | No models |
| `autopilot` | Nemotron-backed narration/prioritization: `Insight`, `PriorityCall`, `AnomalyFlag`, `DraftMessage`, `ReviewedIngestRun` models, `nemotron_client.py`, `services.py`, 5 management commands (see "Autopilot" below) | A third consumer of `csp.services`, same rule as `api`/`dashboard` — reads via services, writes only its own tables |

Why split this way: `api` and `dashboard` are two independent *presentation* layers over one
domain (`csp`) — one machine-readable (JSON, API-key auth), one human-readable (HTML, session
auth) — and `ingestion` is the write side. None of the three import each other; each only imports
`csp`. A change to a chart on the dashboard never touches the API contract, and vice versa.

## Two front doors, one domain layer

The product now has **two consumers of the same data**, deliberately kept separate rather than
merged into one auth scheme:

| | API (`/api/v1/*`) | Dashboard (`/dashboard/*`) |
|---|---|---|
| Audience | other services (mobile apps, other backends, automation) | Eko staff, in a browser |
| Auth | `X-API-Key` header, per-consumer scopes (Phase 8) | Django session cookie, staff login |
| Response | JSON envelope (`data`/`pagination`/`request_id`) | Server-rendered HTML + a small amount of client-side JS |
| Where the key/session lives | in the calling service's own config | in the browser's session cookie — never an API key |

**No API key ever reaches a browser.** `dashboard/views.py` calls `csp.services` functions
directly — the exact same functions `api/v1/*.py` calls — so the two front doors can never drift
in what a number means, but a leaked page-source view of the dashboard leaks nothing a consumer
could replay against the API.

## Autopilot (Nemotron)

A third, background consumer of the same domain layer — narration and call-prioritization over
real numbers already computed elsewhere, not a new front door. Four generation functions
(`autopilot/services.py`), each: reads real data via `csp.services` (never raw CALLING SHEET
text, never invents a figure), sends it to NVIDIA's Nemotron endpoint
(`autopilot/nemotron_client.py`, OpenAI-compatible) with a strict "respond with only this JSON
shape" prompt, validates the shape before trusting it, and produces nothing that run if the key
is missing, the call fails, or the response doesn't validate — never raises into a management
command or a page render.

| Model | What it is | Risk tier |
|---|---|---|
| `Insight` | One narrative paragraph over network-wide numbers for a month | Read-only |
| `PriorityCall` | Ranked call list, reasoning over gap + trend + account growth together | Informational — ops decides who to actually call |
| `AnomalyFlag` | Ingestion runs flagged for a possible data-entry/pipeline error | Observational — never blocks or modifies the run it reviews |
| `DraftMessage` | Per-CSP nudge text | **Draft only.** Status starts and stays `draft` until a human approves it in the admin (`DraftMessageAdmin.approve_drafts`) — there is no messaging-provider integration, so nothing in this codebase ever sends one |

Every generated row is traceable back to a hallucination-proof source: entries referencing a
CSP code Nemotron invented (not present in the candidates sent to it) are silently dropped before
saving — see the tests in `tests/integration/test_autopilot_services.py` for the exact guardrail
each function enforces.

Runs on a schedule inside the `worker-autopilot` container's poll loop
(`ingestion/management/commands/watch_transactions.py`, `--autopilot-interval`, default daily) via
`manage.py run_autopilot` — one process, no new infrastructure, each of its four steps
independently wrapped so one failing (bad response, transient API error) never blocks the rest.

## CSP Operations Agent (multi-agent, separate from the four Nemotron functions above)

A second, later, structurally different AI system: a Coordinator (`autopilot/agent.py`) routing to
7 specialist agents (Data/Balance/Transaction/Risk/Verification/Action/Performance,
`autopilot/agents.py`), each tool-permission-bounded (`AGENT_PERMISSIONS`/`call_tool()` in
`autopilot/agent_tools.py` — e.g. the Balance Agent cannot call a communication tool), producing
structured `AgentFinding`/`AgentVerification`/`AgentAction` rows, never free-form chat. Every
finding is re-verified against the *same canonical service call* that produced it before it can
trigger an `AgentAction`, and every consequential action starts `PENDING_APPROVAL` — a human
approves it (dashboard/admin), the agent never executes autonomously. Missing/conflicting/stale
evidence is represented explicitly (`AgentFinding.VerificationStatus.INSUFFICIENT_DATA`/
`CONFLICTING_DATA`/`STALE_DATA`), never guessed into a false finding.

Triggered manually from AI Operations or on a schedule (`manage.py run_csp_operations_agent`).
Since 2026-09-21, the manual dashboard trigger dispatches through a Redis-backed queue
(`autopilot/tasks.py`, consumed by the `agent-worker` container, `manage.py run_agent_worker`)
instead of running inline on the request thread — the agent's own decision logic is unchanged;
only *where* it executes changed. Falls back to synchronous execution if no `REDIS_URL` is
configured, so it still works with zero new infrastructure in a minimal deployment.

## Historical comparison engine (`csp/comparison.py`)

A fourth thing the domain layer computes, alongside Rule 19 slabs and month-end projections:
date-aware comparisons — today vs. yesterday, same-date-previous-month (with an explicit,
named fallback for short months, never a silently-guessed date), rolling 7-day windows, and
MTD-so-far readiness. One rule underpins every result: a missing reading is `NO_DATA`, never
`DECLINE` — a gap in reporting must never read as a falling balance.

Reuses, never re-implements: `csp.rules` for slab/rate/gap math (the same functions
`MonthlySummary` uses), `DailyBalance` for daily balance history, and the dbt `daily_activity`
mart for daily ONUS/Overall transaction history. The one genuinely new table,
`DailyCspSnapshot`, exists only because a *daily* Rule 19 slab classification didn't exist
before (`MonthlySummary` is month-grain) — it's the MTD-so-far average's slab as of a given
day, not that single day's raw balance, because Rule 19 is fundamentally a monthly-average
rule. Built by `manage.py sync_daily_snapshots` (a worker schedule, batched across every CSP
in a handful of queries — not one query per CSP).

Three consumers read this module and only this module for anything comparison-shaped: the
`/api/v1/comparisons*` endpoints, the dashboard's CSP Daily Comparison tab (plus the Overview
KPI strip, the Trends page's today-vs-yesterday transaction panel, and the CSP detail page's
daily-comparison panel), and — like the rest of the domain layer — nothing else re-derives a
trend, a movement bucket, or an at-risk reason independently.

## Telegram ingestion (`manage.py poll_telegram`)

A second delivery path for the same transaction-sheet ingestion `manage.py ingest_transactions`
already does for the watched folder — never a second ingestion engine. `poll_telegram` polls
the Telegram Bot API (`ingestion/telegram_client.py`), enforces a single-chat allow-list
(`TELEGRAM_CHAT_ID`; refuses to run without one if a bot token is configured), dedupes on
Telegram's own content fingerprint (`file_unique_id`) recorded in `IngestLog.source_hash`,
downloads the file straight into the same watched `incoming/` folder, and hands it to
`ingestion/file_ingest.py::ingest_transaction_file()` — the exact validate/parse/upsert/file
orchestrator `ingest_transactions` itself calls. Every Telegram update (file or not, authorized
or not, duplicate or not) gets an `IngestLog` row so the next poll's offset can be derived from
`IngestLog` itself, without a second, Telegram-specific state table. Degrades to a no-op if
`TELEGRAM_BOT_TOKEN` isn't set — same pattern as Autopilot's `NEMOTRON_API_KEY`.

## Why one image, three containers

`web` and `worker` are built from the identical `Dockerfile` (`csp-balance-tracker:latest`) and
differ only in the command `scripts/entrypoint.sh` runs (`web` vs `worker`). This means:
- one image to build, scan, and patch — not two drifting Dockerfiles
- `worker` never needs a public port, a gunicorn process, or `web`'s healthcheck
- only `web` runs `migrate` on start, so the two containers can't race each other on schema changes

`db` is a stock `postgres:17` image, private to the Compose network, no host port.

## Why dbt for the calculation layer, not plain Django

The MAB/slab/gap/trend rollup (PRD §5.2/FR4) is a set of aggregations over one table, evaluated
once a day — a textbook dbt problem: `dbt test` gives free, git-tracked data assertions (unique
`csp+month`, non-negative balances, valid slab values — 12 tests, all passing against real data),
and `dbt docs` gives lineage from raw ingested rows to the exact number an API consumer sees. This
matters because the number drives a real SBI incentive payout. The month-end **projection** (an
estimate, not a deterministic aggregation) is a small Python module instead (`csp/projection.py`)
— dbt/SQL is the wrong tool for that part. `sync_monthly_summary` is the seam between the two: it
reads dbt's `monthly_summary` mart, calls the projection module, and writes the combined result
into the Django-owned `csp_monthlysummary` table the API actually serves — dbt never writes to a
Django-migrated table directly, so the two tools' schema-ownership never conflicts.

## Request paths

- **`/`** redirects to `/dashboard/`.
- **Dashboard** (`/dashboard/*`): Django session auth, `@login_required` on every view. Staff
  accounts are created with `manage.py createsuperuser` or the admin — not API keys. Two tiers
  within the dashboard itself, both on the same login:
  - **Staff** (`user.is_staff`): everything, including `/dashboard/api-docs/` and
    `/dashboard/admin-console/` (see below).
  - **Viewer** (`is_staff=False`, e.g. `manage.py shell` / admin-created account): Overview, Trends,
    CSP Daily Comparison, and CSP detail only — read-only monitoring, no ingestion/API-integration
    surfaces. The nav in
    `dashboard/templates/dashboard/base.html` hides staff-only links entirely rather than showing
    a dead link; the views (`dashboard/views.py`) enforce the same check server-side, so this is
    access control, not just UI polish.
- **API** (`/api/v1/*`): `X-API-Key` header, checked in `api/auth.py` with a constant-time
  comparison. No end-user login — every consumer is a server-to-server client.
- **Admin** (`/admin/`, plus `/dashboard/admin-console/` which iframes it): Django's own
  session/cookie auth, same login as the dashboard. The dashboard nav's "Admin" link goes to the
  embedded `/dashboard/admin-console/` (staff-only) rather than linking out to `/admin/` directly,
  so the app reads as one surface instead of redirecting into Django's own chrome — same pattern
  as the embedded API docs.
- **Health** (`/health`): no auth, checked by the Docker healthcheck and (recommended) Nginx's
  upstream health probe.

Whether `/dashboard/` and `/admin/` are reachable from outside the R730's LAN is the
administrator's Nginx path-rule decision (docs/DEPLOYMENT.md) — this repo requires login on both,
but doesn't decide their public reachability.
