#!/bin/sh
# Single entrypoint for `web`, `worker`, and `agent-worker` (Phase 7/8,
# extended 2026-09-21 for horizontal scaling) — one image, one place
# startup behaviour is defined, no drift between roles.
set -eu

cd /app/app

case "${1:-web}" in
  web)
    # SKIP_MIGRATE_ON_BOOT: when running >1 `web` replica, N containers
    # racing `migrate --noinput` on the same DB at startup is a real (if
    # narrow) risk — Django doesn't take a cross-process lock for it. The
    # default (unset -> migrate runs, exactly as before this variable
    # existed) is unchanged for single-replica/local use; a multi-replica
    # deploy should run `docker compose run --rm web python manage.py
    # migrate` once as its own step and set SKIP_MIGRATE_ON_BOOT=1 on every
    # `web` replica. See the scalability report's "remaining blockers".
    if [ "${SKIP_MIGRATE_ON_BOOT:-0}" != "1" ]; then
      python manage.py migrate --noinput
    fi
    # whitenoise (STORAGES["staticfiles"]) needs the manifest built once per
    # image/release — dashboard/static/* -> /app/staticfiles. Idempotent.
    python manage.py collectstatic --noinput
    # --workers: concurrency within this replica (scale further via more
    # replicas, not just more workers here — see compose.yml `web`).
    # --timeout/--graceful-timeout: bound how long one request can occupy a
    # worker and give in-flight requests time to finish on shutdown/reload
    # instead of being killed mid-response (graceful shutdown requirement).
    exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-30}" \
      --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}"
    ;;
  worker)
    # WATCH_JOBS: comma list forwarded to --jobs (empty/unset -> all five,
    # identical to pre-2026-09-21 behaviour). See compose.yml for how this
    # splits one worker role into several independently-scalable ones.
    jobs_arg=""
    if [ -n "${WATCH_JOBS:-}" ]; then
      jobs_arg="--jobs ${WATCH_JOBS}"
    fi
    # shellcheck disable=SC2086
    exec python manage.py watch_transactions --interval "${WATCH_INTERVAL_SECONDS:-300}" $jobs_arg
    ;;
  agent-worker)
    # Consumes the "agents" RQ queue (autopilot/tasks.py) — the CSP
    # Operations Agent's async execution path. manage.py run_agent_worker,
    # not the bare `rq worker` CLI: the latter never calls django.setup(),
    # so importing the job function (which imports Django models several
    # layers down) crashes with AppRegistryNotReady — confirmed against a
    # real queued job. No-op idle (never crashes) if REDIS_URL isn't set,
    # mirroring every other "missing integration degrades" case here.
    if [ -z "${REDIS_URL:-}" ]; then
      echo "agent-worker: REDIS_URL not set — nothing to consume, idling." >&2
      exec tail -f /dev/null
    fi
    exec python manage.py run_agent_worker
    ;;
  *)
    echo "Unknown role: $1 (expected 'web', 'worker', or 'agent-worker')" >&2
    exit 1
    ;;
esac
