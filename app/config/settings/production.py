"""
Production settings — this is what `DJANGO_SETTINGS_MODULE` points at inside
the Docker image (see Dockerfile / scripts/entrypoint.sh).

Phase 3 security baseline: DEBUG is hard False, ALLOWED_HOSTS/CSRF are
environment-driven with no permissive default, secure cookies are on, and the
app refuses to start with placeholder secrets instead of silently running
insecurely.
"""

from django.core.exceptions import ImproperlyConfigured

from config.logging import build_logging_config

from .base import *  # noqa: F403
from .base import SECRET_KEY, env

DEBUG = False

# No default: an admin must explicitly set this, otherwise Django's own
# ALLOWED_HOSTS check refuses every request rather than us guessing "*".
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# --- Persistent DB connections (scalability foundation, 2026-09-21) --------
# 0 (Django's default) closes+reopens a connection every request — fine at
# low concurrency, a real bottleneck once request volume grows. 60s reuses
# a connection across requests on the same worker process; CONN_HEALTH_CHECKS
# pings before reuse so a connection Postgres already dropped (e.g. after a
# restart) is transparently replaced instead of surfacing as a request error.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405

# --- Redis cache (scalability foundation, 2026-09-21) -----------------------
# Only takes effect once REDIS_URL is actually set — base.py's LocMemCache
# stays in force otherwise, so a production deploy with Redis not yet
# provisioned still starts and runs correctly (degraded to per-process
# caching only), matching this project's existing "missing integration
# degrades, never crashes" convention (NEMOTRON_API_KEY, TELEGRAM_BOT_TOKEN).
if REDIS_URL:  # noqa: F405
    _cache_options = {
        "CLIENT_CLASS": "django_redis.client.DefaultClient",
        # A Redis outage must degrade the app to "no cache", never
        # to 500s — common/cache.py wraps every call anyway, but
        # this is the backend's own belt-and-braces version of the
        # same fail-open rule.
        "IGNORE_EXCEPTIONS": True,
        "SOCKET_CONNECT_TIMEOUT": 1,
        "SOCKET_TIMEOUT": 1,
    }
    # Redis Sentinel HA (2026-10-06) — VERIFIED against a real 3-sentinel/
    # 1-master/2-replica cluster (scripts/redis-sentinel-verify/), run from
    # inside this project's own image so the exact installed django-redis/
    # redis-py versions were exercised, not just "the config loads". That
    # run caught two real bugs this comment used to describe as an
    # untested guess: (1) LOCATION must be a URL whose *hostname* is the
    # Sentinel master/service name (django_redis.pool.SentinelConnection
    # Factory.get_connection_pool() reads it via url.hostname) — reusing
    # the plain host:port REDIS_URL silently pointed it at the wrong
    # name; (2) OPTIONS.CONNECTION_FACTORY must explicitly be
    # "django_redis.pool.SentinelConnectionFactory" — without it,
    # django-redis falls back to the plain (non-Sentinel) connection
    # factory and fails with a TypeError. See scripts/redis-sentinel-
    # verify/README.md for the exact commands used to prove this.
    _sentinel_hosts = env("REDIS_SENTINEL_HOSTS", default="")  # noqa: F405
    if _sentinel_hosts:
        _sentinel_master_name = env("REDIS_SENTINEL_MASTER_NAME", default="mymaster")  # noqa: F405
        _cache_options["CLIENT_CLASS"] = "django_redis.client.SentinelClient"
        _cache_options["CONNECTION_FACTORY"] = "django_redis.pool.SentinelConnectionFactory"
        _cache_options["SENTINELS"] = [
            tuple(h.rsplit(":", 1)) for h in _sentinel_hosts.split(",")
        ]
        _cache_options["CONNECTION_POOL_CLASS"] = "redis.sentinel.SentinelConnectionPool"
        _redis_location = f"redis://{_sentinel_master_name}/0"
    else:
        _redis_location = REDIS_URL  # noqa: F405
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": _redis_location,
            "OPTIONS": _cache_options,
        }
    }
    # Session reads no longer hit Postgres on every authenticated request;
    # cached_db still writes through to the DB and still works if Redis is
    # down (falls back to the DB-only session backend's behaviour), so this
    # is a pure read-path optimization, not a new availability dependency —
    # sessions/auth remain correct across any number of stateless replicas
    # either way, since neither backend ever stores session state in
    # process memory.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

if SECRET_KEY == "CHANGE_ME_INSECURE_DEV_KEY":  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is unset (or left at its insecure default) while "
        "running config.settings.production. Set a real secret in .env before "
        "starting this service."
    )

# There is no env-var API-key check here anymore — keys are DB-backed
# (api.models.ApiKey, Phase 8). An empty ApiKey table just means no consumer
# can call the API yet, which `manage.py create_api_key` fixes at any time
# without a redeploy — it doesn't need a startup guard.

# --- cookie / transport security --------------------------------------------
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Left False deliberately: Nginx (outside this repo) is the HTTPS edge and
# already redirects HTTP->HTTPS there. Enabling this in Django as well would
# also try to redirect the container's own internal healthcheck
# (`curl http://127.0.0.1:8000/health`, no X-Forwarded-Proto), which would
# then fail `curl -f` on a 301 and break the Docker healthcheck.
SECURE_SSL_REDIRECT = False

SECURE_CONTENT_TYPE_NOSNIFF = True
# X_FRAME_OPTIONS lives in base.py (SAMEORIGIN) — the dashboard embeds its
# own /api/v1/docs in an iframe, so both dev and prod need the same value.

# HSTS is a deliberate follow-up, not enabled yet: it should only go on once
# the domain + TLS cert are live and confirmed stable (a bad HSTS header is
# hard to walk back for returning visitors). See docs/DEPLOYMENT.md.

# --- Logging (Phase 12) ------------------------------------------------------
# JSON lines in production so the R730's log tooling can parse/filter by
# request_id, consumer, stage, job_id.
LOGGING = build_logging_config(json_output=True)
