# Deployment — CSP Average Balance Tracker

Target: **Eko Dell PowerEdge R730** (`192.168.1.4`), per the `r730-deployment-standards` house
standard. Developers prepare this repository; an authorized administrator performs the actual
`docker compose up` on the server. See PRD.md §12a for how this was derived.

## Project

| | |
|---|---|
| Project name | `csp-balance-tracker` |
| Project root on server | `/opt/projects/csp-balance-tracker` |
| Repository | this repo (application code + deployment files together) |

## Services / containers

One image (`csp-balance-tracker:latest`), scaled horizontally as of the 2026-09-21 scalability
pass — see the scalability report for the full rationale. `web` and each `worker-*` role can run
any number of replicas via `docker compose up -d --scale <service>=N`; every other row is a single
instance.

| Container | Role | Purpose |
|---|---|---|
| `csp-balance-tracker-app-lb` | `lb` | nginx — the **only** published port. Load-balances across every `web` replica via Docker's embedded DNS (see `scripts/nginx/nginx.conf`) |
| `web` (N replicas, no fixed name) | `web` | Django: staff dashboard (`/dashboard`), django-ninja API (`/api/v1`), admin console (`/admin`), `/health`, `/metrics` — gunicorn, static files served by whitenoise. Stateless: no replica-specific state, reachable only through `lb` |
| `csp-balance-tracker-app-worker-ingestion` | `worker` (`WATCH_JOBS=ingest_transactions`) | Watched-folder transaction ingestion only |
| `csp-balance-tracker-app-worker-calling-sheet` | `worker` (`WATCH_JOBS=ingest_calling_sheet,sync_daily_snapshots`) | CALLING SHEET poll + daily snapshot rebuild |
| `csp-balance-tracker-app-worker-telegram` | `worker` (`WATCH_JOBS=poll_telegram`) | Telegram transaction-sheet ingestion only |
| `csp-balance-tracker-app-worker-autopilot` | `worker` (`WATCH_JOBS=run_autopilot`) | Daily Nemotron autopilot pass only |
| `csp-balance-tracker-app-agent-worker` | `agent-worker` | Consumes the CSP Operations Agent's async "agents" RQ queue (`manage.py run_agent_worker`) — idles harmlessly if `REDIS_URL` is unset |
| `csp-balance-tracker-app-redis` | `redis` | Cache / rate-limit / job-queue backend — **never authoritative data**, disposable, no volume |
| `csp-balance-tracker-app-db` | `db` | PostgreSQL 17 — **not publicly exposed** |

All five `worker`-role containers run the exact same command/image as the original single `worker`
service (`manage.py watch_transactions`), just each restricted via `--jobs`/`WATCH_JOBS` to a
subset of its five schedules — same code, same per-job behaviour, split so each can be scaled
independently. Running one instance with `WATCH_JOBS` unset still runs all five schedules in one
process, exactly as before this split existed, if that's ever preferred for a smaller deployment.

## Ports

| | |
|---|---|
| Host port (published) | **`lb` only** — `127.0.0.1:${APP_PORT}` (pending administrator assignment, range `8200-8299`) |
| `web` | `expose`d on `8000` internally, no host port — reachable only via `lb` or from other containers on `app-network` |
| `worker-*`, `agent-worker` | no published port — not HTTP services |
| Postgres, Redis | no host port published; internal `app-network` only |

Public flow is now `Internet → (R730's own Nginx, outside this repo) → 127.0.0.1:${APP_PORT} →
lb (nginx, in this repo) → web replica`. The R730's outer Nginx doesn't need to know `lb` exists —
it's still proxying to one upstream on `127.0.0.1:${APP_PORT}`, which now happens to itself be a
load balancer instead of a single Django process.

## Health endpoints (liveness/readiness split, P0 fast-pass 2026-09-21)

| Endpoint | Checks | Use for |
|---|---|---|
| `GET /health/live` | Nothing but "the process can respond" — no DB call | Container/orchestrator liveness (Docker HEALTHCHECK, compose healthchecks) — must never fail on a transient DB blip |
| `GET /health/ready` | DB connectivity + CALLING SHEET ingest staleness | Load balancer / Nginx upstream probe — stop routing to a replica that can't actually serve, without killing it |
| `GET /health` | Identical to `/health/ready` | Kept for backward compatibility with anything already pointed at it |

No auth on any of the three.

## Required environment variables

Full, current list: [.env.example](../.env.example). Notable:

- `DJANGO_SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_urlsafe(50))"`, not a short/predictable string (`manage.py check --deploy` will flag a weak one).
- `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` — set to the real domain once assigned.
- `DATABASE_URL` (+ `DATABASE_NAME/USER/PASSWORD` for the `db` container)
- `GOOGLE_SERVICE_ACCOUNT_FILE_HOST`, `CALLING_SHEET_ID`
- `TRANSACTION_DATA_DIR_HOST`
- `APP_NAME`, `APP_PORT`

API keys are **not** environment variables — they are per-consumer DB rows (`api.models.ApiKey`,
hashed). After the first `migrate`, issue one per consumer:
`docker compose exec web python manage.py create_api_key <name> --scopes <...> --label prod`
(see docs/API-CONSUMERS.md).

`.env` is never committed. `config.settings.production` (what the image runs) deliberately
**refuses to start** if `DJANGO_SECRET_KEY` is left at its insecure placeholder or if both API key
settings are empty — see `app/config/settings/production.py`.

## Secrets

**The Google service-account JSON key must never live inside this git repository — not even
gitignored.** It lives on the host, outside `/opt/projects/csp-balance-tracker`, and is bind-mounted
**read-only** into the `worker` container:

```yaml
# compose.yml (already configured)
worker:
  volumes:
    - ${GOOGLE_SERVICE_ACCOUNT_FILE_HOST}:/run/secrets/google-sa.json:ro
```

Recommended host path: `/opt/secrets/csp-balance-tracker/google-sa.json` (outside the project
root). Set `GOOGLE_SERVICE_ACCOUNT_FILE_HOST` in `.env` to wherever it actually is. Transfer it to
the server out-of-band (`scp`/admin file transfer), never via git.

If the key is ever exposed, rotate it:

```bash
gcloud iam service-accounts keys create secrets/new-key.json \
  --iam-account=csp-balance-tracker@eko-502406.iam.gserviceaccount.com
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
  --iam-account=csp-balance-tracker@eko-502406.iam.gserviceaccount.com
```

## File storage (transaction ingestion)

Production input files must also live outside the git repo. Recommended host path:

```
/opt/data/csp-balance-tracker/
  incoming/    <- the watched folder (drop the daily .xlsx export here)
  processed/   <- successfully ingested files land here (never deleted)
  failed/      <- rejected files land here for review (never deleted)
```

Set `TRANSACTION_DATA_DIR_HOST=/opt/data/csp-balance-tracker` in `.env`; compose bind-mounts it to
`/data` inside the `worker` container. The repository itself never contains production input
files — only the empty `data/incoming|processed|failed/.gitkeep` skeleton, for local dev.

### R730: syncing from the real transaction sheet (2026-10-06)

On the R730, the real transaction data isn't dropped as discrete files — it's a continuously-updated
monthly master workbook (e.g. `/home/shumit/sbikisok/Transaction Oct'26.xlsx`) owned by a different
team. `scripts/sync_transaction_sheet.sh` bridges this: it only *reads* that file (never writes to
the source directory) and copies the current month's workbook into this app's own `incoming/`, where
the normal `ingest_transactions` job picks it up as usual. Re-syncing the whole file periodically and
re-ingesting is safe — upserts are idempotent (see `tests/unit/test_transaction_ingest.py::test_reingesting_same_file_is_idempotent`).

Run via cron on the host (not inside a container, so it has access to the source directory):

```cron
0 */4 * * * TRANSACTION_DATA_DIR_HOST=/opt/data/csp-balance-tracker /path/to/repo/scripts/sync_transaction_sheet.sh >> /var/log/csp-balance-tracker-txn-sync.log 2>&1
```

`TRANSACTION_SOURCE_DIR` (default `/home/shumit/sbikisok`) and `TRANSACTION_SYNC_RETENTION_DAYS`
(default `14` — how long synced copies are kept in `processed/` before being pruned, since this
re-syncs the same file repeatedly rather than receiving distinct uploads) are both overridable.

## Volumes

| Volume | Mounted at | Purpose |
|---|---|---|
| `db_data` (named) | `/var/lib/postgresql/data` in `db` | Postgres data — persistent |
| `${TRANSACTION_DATA_DIR_HOST}` (bind) | `/data` in `worker` | transaction file workflow — persistent, host-owned |
| `${GOOGLE_SERVICE_ACCOUNT_FILE_HOST}` (bind, read-only) | `/run/secrets/google-sa.json` in `worker` | credential |

## Connection pooling (PgBouncer)

`pgbouncer` (compose.yml) is a **real, verified-working, opt-in** service — confirmed live by
connecting through it and querying real data, not just started-and-assumed. It is **not** the
default `DATABASE_URL` target: switching to it means setting, on `web`/`worker-*`/`agent-worker`:

```
DATABASE_URL=postgres://appuser:<password>@pgbouncer:5432/csp_balance_tracker
```

(note: this image listens on `5432` internally, not the pgbouncer-standard `6432` — confirmed
against the running container). `AUTH_TYPE=scram-sha-256` is required — Postgres 17 defaults to
scram-sha-256 password encryption, and pgbouncer's own default (md5-hashing the password itself)
fails auth against it with "wrong password type" (hit and fixed live while verifying this).

**Migrations should still run directly against `db:5432`**, not through pgbouncer's transaction
pooling — `manage.py migrate` is a one-off, low-frequency operation with no pooling benefit and
some migration operations don't play well with transaction-mode pooling.

Local dev/single-instance: skip this — `DB_CONN_MAX_AGE` (production.py) already gives connection
reuse without another moving part. Add PgBouncer once enough `web`/worker replicas exist that their
combined direct connection count risks nearing Postgres's own `max_connections`.

## Migration command

```bash
docker compose exec web python manage.py migrate
```

(Also runs automatically on every `web` container start — see `scripts/entrypoint.sh`. `worker`
never runs migrations, avoiding a race between the two containers.)

## Ingestion / calculation commands

Since the scalability foundation (2026-09-21), the single `worker` role is split into four
independently-scalable services (see `compose.yml`'s `x-worker` anchor and each service's
`WATCH_JOBS` env var) — there is no service literally named `worker` to `exec` into.

| Command | Runs in | Trigger |
|---|---|---|
| `manage.py ingest_calling_sheet` | `worker-calling-sheet` (its own `watch_transactions` loop) | continuous, every 60s (`--calling-sheet-interval`) — not cron |
| `manage.py watch_transactions` | every `worker-*` service (its main process) | continuous — each container's own command, running only the jobs in its `WATCH_JOBS` |
| `manage.py ingest_transactions` | `worker-ingestion` (its own loop) | continuous, every 5 min (`--interval`, default); also triggered indirectly by `scripts/sync_transaction_sheet.sh` dropping a fresh file into `incoming/` |
| `manage.py sync_daily_snapshots` | `worker-calling-sheet` (its own loop) | continuous, every 5 min (`--snapshot-interval`, default); can also be run once manually with `--date` |
| `manage.py poll_telegram` | `worker-telegram` (its own loop) | continuous, every 60s (`--telegram-interval`, default); no-op unless `TELEGRAM_BOT_TOKEN` is set |
| `scripts/sync_transaction_sheet.sh` | host cron (not a container) | every 20 min on the R730 — copies the live source workbook into `incoming/`, see "R730: syncing from the real transaction sheet" above |
| `dbt run` / `dbt test` | any `worker-*` container (dbt-core is in the shared image) | host cron |
| `manage.py sync_monthly_summary` | `web` or any `worker-*` | host cron, right after `dbt run` |

Status: **every command in this table is implemented and verified against real data** (the live
CALLING SHEET + real multi-month transaction history) — `ingest_calling_sheet` matches columns by
header text and parses Indian-formatted currency, `ingest_transactions` bulk-upserts idempotently,
the dbt marts pass all 12 tests, and `sync_monthly_summary` writes the projected
`csp_monthlysummary` rows the API reads. See docs/DATA-FLOW.md for the verified numbers.

## Scheduled jobs

The R730 production deployment runs `dbt run` + `sync_monthly_summary` every 20 minutes, staggered
a few minutes behind the transaction sync so each step has time to finish before the next depends
on it (`worker-calling-sheet` is just the container chosen to `exec` into — any `worker-*` service
works, since dbt-core and the Django app are in the same shared image):

```cron
*/20 * * * * TRANSACTION_DATA_DIR_HOST=/path/to/data /path/to/scripts/sync_transaction_sheet.sh >> /path/to/txn_sync.log 2>&1
8,28,48 * * * * docker compose -f /path/to/compose.yml exec -T -w /app/dbt worker-calling-sheet dbt run --project-dir /app/dbt --profiles-dir /app/dbt >> /path/to/dbt_run.log 2>&1
10,30,50 * * * * docker compose -f /path/to/compose.yml exec -T worker-calling-sheet python app/manage.py sync_monthly_summary >> /path/to/dbt_run.log 2>&1
```

`dbt`'s connection env vars (`DATABASE_HOST=db`, etc.) reach the container the same way the app's
do — through `.env`, loaded automatically by `env_file:` in compose. `ingest_calling_sheet`,
`ingest_transactions`, `sync_daily_snapshots`, and `poll_telegram` need no cron entry — they're all
schedules inside their own `worker-*` container's long-running `watch_transactions` loop.

## Deployment commands

```bash
cd /opt/projects/csp-balance-tracker
git pull
cp .env.example .env   # first deploy only; then edit with real values (incl. REDIS_URL)
docker compose config
docker compose up -d --build
# Scale the stateless web tier independently of everything else, any time:
docker compose up -d --scale web=3
docker compose ps
curl http://127.0.0.1:${APP_PORT}/health
```

## Validation commands

```bash
docker compose ps                                    # lb/web/redis/db/worker-*/agent-worker all healthy
curl http://127.0.0.1:${APP_PORT}/health              # {"status": "ok", ...} — via lb
curl http://127.0.0.1:${APP_PORT}/metrics             # Prometheus text — per-process counters, see the scalability report
docker compose logs worker-calling-sheet --tail=50    # CALLING SHEET + daily snapshot polling
docker compose logs worker-ingestion --tail=50        # transaction-file polling
docker compose logs agent-worker --tail=50            # CSP Operations Agent async queue
docker compose exec web python manage.py check --deploy
```

## Rollback

```bash
git log --oneline -5          # find the previous good commit
git checkout <previous-sha>
docker compose up -d --build
```

Database rollback is not automated — see backup/restore below before rolling back a release that
included a migration.

## Backup / restore (Postgres)

```bash
# Backup
docker compose exec db pg_dump -U ${DATABASE_USER} ${DATABASE_NAME} > backup_$(date +%F).sql

# Restore
cat backup_YYYY-MM-DD.sql | docker compose exec -T db psql -U ${DATABASE_USER} ${DATABASE_NAME}
```

Recommended cadence: nightly `pg_dump`, retain 30 days (PRD §13).

## Nginx routing target (request for the administrator)

```
Domain:          csp-balance-tracker.developer.eko.co.in   (pending confirmation)
Service:         csp-balance-tracker-app (the `web` container)
Upstream:        127.0.0.1:<HOST_PORT>   (pending assignment)
Path rules:      / -> upstream, no rewrites needed
TLS required:    yes
Health endpoint: /health
Required header: X-Forwarded-Proto: https  (Django uses this to detect the original scheme — see
                 config/settings/base.py SECURE_PROXY_SSL_HEADER)
```

Public flow: `Internet → Nginx :80/:443 → 127.0.0.1:<HOST_PORT> → csp-balance-tracker-app (web) container`.
This repo does not touch Nginx config; the block above is the handoff request for the R730
administrator to configure it.

## Known operational risks

- **CALLING SHEET layout can change** — `ingest_calling_sheet` matches its 8 required columns by
  exact header text on row 2 of "Calling Sheet New" (not fixed index), but if a header is renamed
  or that worksheet is renamed/removed, ingestion fails clearly (`CallingSheetValidationError`,
  logged) rather than silently reading the wrong column. Watch `IngestLog` after any sheet
  restructuring on the business side.
- **A few CSP mobile-number cells contain more than one number** (confirmed on live data — 15 of
  539 rows, format e.g. `"9999999999/ 8888888888"` — illustrative numbers, not a real CSP's);
  `ingest_calling_sheet` keeps the first and preserves the raw cell in
  `Csp.raw_attrs.sheet_mobile_raw` for audit.
- **APP_PORT and domain are unassigned** — deployment cannot proceed to the public-Nginx step
  until the administrator assigns both.
- **HSTS is intentionally not enabled** — add `SECURE_HSTS_SECONDS` once the domain + TLS
  certificate are confirmed stable (see `config/settings/production.py`).
- **dbt-postgres only** — `sync_monthly_summary`'s mart query and dbt's own SQL use Postgres-only
  syntax; there is no SQLite fallback for the calculation layer (matches the stated production
  target — see PRD §12).
- **Scaling `web` beyond 1 replica**: each replica runs `migrate --noinput` on boot by default: N
  replicas starting simultaneously against a *new* migration is a narrow but real race (Django
  doesn't take a cross-process migration lock). Set `SKIP_MIGRATE_ON_BOOT=1` on every `web`
  replica and run `docker compose run --rm web python manage.py migrate` once as its own step
  instead, once running more than one `web` replica in production.

## Creating dashboard/staff accounts

The dashboard (`/dashboard/*`) and admin (`/admin/*`) share the same Django login. There is no
self-registration — an existing admin creates each account:

```bash
# Full access (admin + dashboard):
docker compose exec web python manage.py createsuperuser

# Dashboard-only staff (no Django admin access): create via the admin's
# Users page with "Staff status" checked and "Superuser status" unchecked.
```

## Admin handoff (fill in once port/domain are assigned)

```
Project:        csp-balance-tracker
Developer:      biswajit.chacko@gmail.com
Host port:      <assigned port>
Container port: 8000
Binding:        127.0.0.1
Domain:         csp-balance-tracker.developer.eko.co.in (pending confirmation)
Health:         /health
Database:       PostgreSQL 17, private Compose network, named volume db_data
Services:       web (dashboard+API+admin), worker (ingestion), db (Postgres)
Secrets path:   <GOOGLE_SERVICE_ACCOUNT_FILE_HOST, outside the repo>
Data path:      <TRANSACTION_DATA_DIR_HOST, outside the repo, e.g. /opt/data/csp-balance-tracker>
Deployment command:
  docker compose config
  docker compose up -d --build
Validation:
  docker compose ps
  curl http://127.0.0.1:<assigned-port>/health
```
