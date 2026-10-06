# Operations — CSP Average Balance Tracker

Day-to-day running of the service once deployed. For first deployment, see
[DEPLOYMENT.md](DEPLOYMENT.md).

## Checking the service is healthy

Liveness/readiness are separate endpoints (2026-09-21 hardening pass) — see
[DEPLOYMENT.md](DEPLOYMENT.md#health-endpoints-livenessreadiness-split-p0-fast-pass-2026-09-21):

```bash
curl http://127.0.0.1:${APP_PORT}/health/live    # process up? never checks the DB
curl http://127.0.0.1:${APP_PORT}/health/ready   # DB + CALLING SHEET freshness
curl http://127.0.0.1:${APP_PORT}/metrics        # Prometheus text — per-process counters
```

```json
{"status": "ok", "db_ok": true, "calling_sheet_last_success": "...", "calling_sheet_stale": true}
```

`calling_sheet_stale: true` means no successful `ingest_calling_sheet` run has completed recently
(default staleness window: 26h) — check `worker-calling-sheet`'s logs first.

```bash
docker compose ps          # lb/web(xN)/redis/db/worker-*/agent-worker should show healthy/running
docker compose logs web --tail=100
docker compose logs worker-calling-sheet --tail=100   # CALLING SHEET + daily snapshot poll activity
docker compose logs worker-ingestion --tail=100       # transaction-file poll activity
docker compose logs agent-worker --tail=100           # CSP Operations Agent async queue
docker compose logs db --tail=50
```

The single `worker` role from before the 2026-09-21 scalability pass is now four independently
scalable services (`worker-ingestion`, `worker-calling-sheet`, `worker-telegram`,
`worker-autopilot`) — same commands, same behavior, just split so one can be scaled without the
others. See DEPLOYMENT.md's "Services / containers" table.

## Reading ingestion history

Every ingestion run (calling sheet, transactions, or telegram) writes one `IngestLog` row —
`source`, `status`, `rows_read/valid/rejected/upserted`, `error_summary`, plus (for Telegram)
`source_hash` (dedup fingerprint) and `external_ref` (the Telegram update_id). Browse it in the
Django admin (`/admin/csp/ingestlog/`) or:

```bash
docker compose exec web python manage.py shell -c \
  "from csp.models import IngestLog; [print(l.source, l.status, l.started_at, l.error_summary) for l in IngestLog.objects.order_by('-started_at')[:20]]"
```

## Running an ingestion command manually

Ingestion commands need the `/data` bind mount, so run them in a `worker-*` container, not `web`:

```bash
# One-off transaction ingest (normally worker-ingestion's watch_transactions loop does this every 5 min)
docker compose exec worker-ingestion python manage.py ingest_transactions

# CALLING SHEET pull (normally worker-calling-sheet's loop does this every 60s)
docker compose exec worker-calling-sheet python manage.py ingest_calling_sheet
```

## Running the calculation layer manually

```bash
docker compose exec -w /app/dbt worker-calling-sheet dbt run
docker compose exec -w /app/dbt worker-calling-sheet dbt test
docker compose exec web python manage.py sync_monthly_summary
```

(any `worker-*` container has dbt installed — same image; `worker-calling-sheet` is just a
reasonable default since it also owns the CALLING SHEET data dbt's marts read.)

`dbt` reads its Postgres connection from the same `DATABASE_*` env vars as the app (`dbt/profiles.yml`
defaults `DATABASE_HOST` to `db`, matching the compose service name — no extra config needed).

## Running the daily-snapshot / Telegram commands manually

```bash
# Rebuild every CSP's DailyCspSnapshot (MTD-so-far slab) for one business day — safe to re-run,
# every write is an upsert. Normally worker-calling-sheet's loop does this every 5 min.
docker compose exec worker-calling-sheet python manage.py sync_daily_snapshots --date 2026-09-18

# One Telegram poll (normally worker-telegram's loop does this every 60s). No-ops with a message if
# TELEGRAM_BOT_TOKEN isn't set; raises CommandError if the token is set but TELEGRAM_CHAT_ID isn't.
docker compose exec worker-telegram python manage.py poll_telegram
```

## Getting the Telegram chat_id (one-time setup)

`TELEGRAM_BOT_TOKEN` alone isn't enough — `poll_telegram` refuses to run without
`TELEGRAM_CHAT_ID` (an allow-list of exactly one chat; without it, any chat that discovers the
bot could submit transaction files). To find it:

1. From the phone/account that will send transaction sheets, send any message to the bot
   directly (a private chat, not a group).
2. `curl "https://api.telegram.org/bot<TOKEN>/getUpdates"` — the reply's
   `result[].message.chat.id` is the chat_id.
3. Set `TELEGRAM_CHAT_ID` in `.env` to that value and restart `worker-telegram`
   (`docker compose restart worker-telegram`).

## Issuing / rotating / revoking an API key

Keys are per-consumer DB rows (`api.models.ApiKey`), stored only as a SHA-256 hash. No redeploy
is needed for any of these — they take effect immediately.

```bash
# Issue (prints the raw key ONCE — copy it, it is never shown again)
docker compose exec web python manage.py create_api_key dashboard --scopes csp:read,balance:read --label prod

# Rotate: issue the new key, hand it over, confirm the consumer works, THEN revoke the old one
docker compose exec web python manage.py revoke_api_key <old-key-prefix>

# See who has what (prefix, scopes, last_used_at — never the hash or raw key)
#   Django admin -> API -> Api keys
```

Update docs/API-CONSUMERS.md on every issue/rotate/revoke. Never log or paste a raw key into a
ticket/chat — `api/auth.py` compares it, never logs it.

## Tracing a request

Every response carries `X-Request-ID`; every error body carries the same value as `request_id`.
Grep the `web` container's logs for it to find the matching structured line
(`stage=API request_id=… consumer=… route=… status=… duration_ms=…`). Ingestion jobs are traced
by `job_id` (= the `IngestLog` row id) across their `stage=INGESTION/VALIDATION/DATABASE` lines.

## Rotating the Google service-account key

```bash
gcloud iam service-accounts keys create /opt/secrets/csp-balance-tracker/google-sa-new.json \
  --iam-account=csp-balance-tracker@eko-502406.iam.gserviceaccount.com
# point GOOGLE_SERVICE_ACCOUNT_FILE_HOST at the new file, redeploy worker, confirm it works, then:
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
  --iam-account=csp-balance-tracker@eko-502406.iam.gserviceaccount.com
```

## Handling a rejected transaction file

Rejected files move to `data/failed/` (or `${TRANSACTION_DATA_DIR_HOST}/failed/` on the server) —
**never deleted**. To investigate:

1. Find the matching `IngestLog` row (`source=transactions`, `status=failed`) — `error_summary`
   has the exact reason (wrong extension, missing sheet/column, corrupt workbook, etc.).
2. Fix the source export or (if the allow-list itself needs to change) update
   `ingestion/xlsx_validation.py` and its tests — that file is the single place the 7-type
   allow-list and required columns are defined.
3. Drop a corrected copy into `data/incoming/` — `watch_transactions` will pick it up on its next
   poll (default every 5 minutes).

## Restarting a single service

```bash
docker compose restart worker-ingestion worker-calling-sheet worker-telegram worker-autopilot
docker compose up -d --scale web=3    # scale the stateless web tier independently, any time
docker compose up -d web              # after editing .env values web reads (API keys, secrets, etc.)
```

## Backups

See docs/DEPLOYMENT.md "Backup / restore (Postgres)". Recommended: nightly `pg_dump` via host
cron, retain 30 days.

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health` → `db_unreachable` | `db` container down or `DATABASE_URL` wrong | `docker compose ps`, check `db` healthcheck |
| API returns 401 | Missing/wrong/revoked `X-API-Key` | Check the key is active in admin -> API keys; re-issue with `create_api_key` if needed |
| API returns 403 | Key valid but lacks the endpoint's scope | Issue a key with the required scope (docs/API-CATALOG.md lists each endpoint's scope) |
| `worker` container keeps restarting | Bad `TRANSACTION_DATA_DIR_HOST` / `GOOGLE_SERVICE_ACCOUNT_FILE_HOST` bind mount path | `docker compose logs worker`; confirm both host paths exist |
| `web` won't start, log says `ImproperlyConfigured` | Placeholder `DJANGO_SECRET_KEY` in production | Set real values in `.env` — this is `config/settings/production.py` refusing to start insecurely, by design |
| File dropped in `incoming/` never processed | `worker` not running, or wrong `TRANSACTION_DATA_DIR_HOST` | `docker compose logs worker`; confirm the bind mount points at the same host folder you're dropping files into |
| `poll_telegram` keeps failing in worker logs | `TELEGRAM_BOT_TOKEN` set but `TELEGRAM_CHAT_ID` isn't | Set `TELEGRAM_CHAT_ID` (see "Getting the Telegram chat_id" above) and restart `worker` |
| Sent a file to the bot but it never appears in `incoming/` | Wrong `TELEGRAM_CHAT_ID`, or the message wasn't a direct document attachment | Check for an `IngestLog` row with `source=telegram, status=invalid_source` — `error_summary` says whether it was an unauthorized chat_id or not a document |
| Daily Comparison tab shows `NO_DATA` everywhere for a real CSP | `sync_daily_snapshots` hasn't run yet for that date, or no historical `DailyBalance` exists that far back | Run `manage.py sync_daily_snapshots --date <date>` manually; `NO_DATA` before real history exists is correct behavior, not a bug |
