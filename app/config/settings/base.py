"""
Shared settings for the CSP Average Balance Tracker.

Nothing environment-specific (DEBUG, ALLOWED_HOSTS, cookie security, etc.)
lives here — see `development.py` / `production.py`. Values come from
environment variables (see `.env.example`), never hard-coded — R730
standard: "Keep configuration outside source code using environment
variables."
"""

from pathlib import Path

import environ

from config.logging import build_logging_config

# app/config/settings/base.py -> repo root is 3 parents up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
APP_DIR = BASE_DIR / "app"

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="CHANGE_ME_INSECURE_DEV_KEY")

# Read for the dashboard's environment badge (dashboard/context_processors.py)
# only — never branched on for security/behavior decisions (DEBUG is what
# actually governs those, per settings module).
APP_ENV = env("APP_ENV", default="local")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "csp",
    "api",
    "ingestion",
    "common",
    "dashboard",
    "autopilot",
    "bankflow",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "common.middleware.CompressMiddleware",  # gzip dynamic pages/JSON; static is pre-compressed
    # Order matters — see api/middleware.py docstring: logging outer, request-id inner.
    "api.middleware.RequestLoggingMiddleware",
    "api.middleware.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.context_processors.dashboard_shell",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database (PostgreSQL in every real environment; see PRD §10/§12) ------
# CONN_MAX_AGE/CONN_HEALTH_CHECKS: persistent connections instead of one
# TCP+auth handshake per request — a real per-request cost that multiplies
# with concurrency. 0/no health-check remains the default (identical to
# before this setting existed) unless DB_CONN_MAX_AGE is set; production.py
# turns this on explicitly rather than defaulting it here, since a stale
# pooled connection behaving badly is exactly the kind of thing that should
# be an opt-in, reviewed choice, not a silent base-settings default.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="sqlite:///" + str(BASE_DIR / "dev.sqlite3"),
    )
}

# Read-replica scaffold — see common/db_router.py. With no
# DATABASE_REPLICA_URL set (the default everywhere today), "replica" is
# just a second alias for the exact same database as "default": using it
# changes nothing about where data comes from, it only makes `.using
# ("replica")` a valid, already-wired no-op ahead of a real replica
# existing. Never introduces a second physical database on its own.
#
# TEST/MIRROR: PrimaryReplicaRouter.allow_migrate() correctly refuses to
# migrate "replica" (a real replica is never migrated by Django — it gets
# schema changes via physical replication). Under `manage.py test`/pytest,
# that means Django's test runner would otherwise create a SEPARATE,
# never-migrated test database for the "replica" alias -> every
# .using("replica") query fails with "no such table" even though, in this
# no-real-replica-configured state, it's meant to be the exact same
# database as "default". TEST.MIRROR tells the test runner to reuse
# "default"'s already-migrated test database for "replica" instead of
# creating/migrating a second one — confirmed necessary by hitting exactly
# that failure while adding test coverage for this scaffold.
_replica_url = env("DATABASE_REPLICA_URL", default="")
DATABASES["replica"] = (
    env.db_url_config(_replica_url)
    if _replica_url
    else {**DATABASES["default"], "TEST": {"MIRROR": "default"}}
)

DATABASE_ROUTERS = ["common.db_router.PrimaryReplicaRouter"]

# --- Cache (Redis in production; in-process LocMemCache everywhere else) ---
# How long a cached dashboard aggregate may live. It is only a CEILING: every ingest/calculation
# that really changes data bumps the cache generation (common/cache.py), so a long value costs no
# freshness and keeps pages warm between changes. Seconds.
DASHBOARD_CACHE_TTL = env.int("DASHBOARD_CACHE_TTL", default=600)

# REDIS_URL blank -> LocMemCache: dev/test/CI need no Redis instance running
# to pass, matching the existing "blank env var -> graceful degrade" pattern
# used for NEMOTRON_API_KEY below. production.py switches
# to django_redis once REDIS_URL is set — see common/cache.py for the
# fail-open read/write wrapper every cache call actually goes through
# (nothing reads/writes `django.core.cache.cache` directly outside that
# module and this settings file).
REDIS_URL = env("REDIS_URL", default="")
# Explicit Any-valued annotation: production.py reassigns this with a
# nested "OPTIONS" dict, which a narrower str->str inference (from this
# module's own simpler LocMemCache literal) would otherwise reject.
CACHES: dict[str, dict[str, object]] = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "csp-tracker-default",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"

# PRD §13: "today" / "as-of" is always IST.
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# Subpath mounting (R730 deployment, 2026-10-06) — set only when this app is
# reverse-proxied under a path prefix (e.g. https://host/csp-balance-intelligence/)
# instead of at its own domain root. Empty by default: no behaviour change
# for local dev or any root-mounted deployment. When set, Django's own
# reverse()/redirect()/{% url %} output (and the admin) automatically gain
# the prefix; the proxy in front of this app must strip the prefix before
# forwarding the request (same convention as this app's own PATH_INFO).
URL_PREFIX = env("URL_PREFIX", default="")
if URL_PREFIX:
    FORCE_SCRIPT_NAME = URL_PREFIX

# A relative STATIC_URL ("static/") only resolves correctly in the browser
# from pages at exactly one path depth — it silently breaks on any deeper
# page (e.g. /dashboard/csp/<code>/), since the browser resolves it against
# the *current page's* URL, not the site root. Absolute + prefix-aware
# fixes that for every page depth. whitenoise already auto-strips
# FORCE_SCRIPT_NAME from its own internal match prefix (see
# WhiteNoiseMiddleware.__init__), so it correctly matches the
# proxy-stripped incoming request path either way — no further wiring
# needed on that side.
STATIC_URL = f"{URL_PREFIX}/static/" if URL_PREFIX else "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Dashboard auth (Phase: "full web app") ---------------------------------
# Server-rendered, staff-login-gated — see dashboard/views.py. Uses Django's
# own session auth; issue accounts with `manage.py createsuperuser` (or a
# non-superuser staff account via the admin) rather than API keys, which are
# for the separate machine-to-machine /api/v1 surface.
LOGIN_URL = "dashboard:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "dashboard:login"

# SAMEORIGIN (not Django's default DENY): the dashboard embeds /api/v1/docs
# in an iframe so API docs feel like part of the app rather than a
# disconnected page. Still blocks every other site from framing us — this
# only permits our own pages to frame our own pages.
X_FRAME_OPTIONS = "SAMEORIGIN"

# --- CSRF / proxy awareness --------------------------------------------------
# Nginx (outside this repo) terminates TLS and proxies to this app over plain
# HTTP on the loopback interface. This header is how Django learns the
# *original* request was HTTPS — needed for correct secure-cookie behaviour
# and CSRF origin checks. Requires Nginx to set `X-Forwarded-Proto` (documented
# in docs/DEPLOYMENT.md's Nginx handoff section).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# CORS is intentionally NOT enabled on /api/v1 — every *API* consumer is a
# server-to-server client authenticating with X-API-Key (PRD §9), cross-origin
# or not. The dashboard app is a same-origin, session-authenticated consumer
# of the domain layer (not the API), so it needs no CORS exemption either.
# Add django-cors-headers only if a *separate-origin* browser app needs the
# API directly, with an explicit allow-list, never "*".

# --- Application-specific config (PRD §7-§9) --------------------------------
GOOGLE_SERVICE_ACCOUNT_FILE = env("GOOGLE_SERVICE_ACCOUNT_FILE", default="")
CALLING_SHEET_ID = env("CALLING_SHEET_ID", default="")
CALLING_SHEET_WORKSHEET_NAME = env("CALLING_SHEET_WORKSHEET_NAME", default="Calling Sheet New")

# New PMJDY accounts: every month the main tab (CALLING_SHEET_WORKSHEET_NAME) gains blocks such as
# "MTD Achievement October'26" / "FTD Achievement ..." / "Targets Oct 2026" with a PMJDY column.
# The block for the current IST month is read by ingest_pmjdy (bankflow.pmjdy_monthly).
# Rate-limited: within PMJDY_MIN_INTERVAL_SECONDS of the last success it does nothing.
PMJDY_COLUMN = env("PMJDY_COLUMN", default="PMJDY")

# "Needs attention" materiality. A CSP is flagged for more money going out than coming in only
# when the imbalance is both large in rupees AND a large share of that CSP's own activity (a 2 %
# wobble on ten lakh of turnover is noise). Alerts pause, with a visible notice, when statements
# cover too little of the period for a CSP-level comparison to mean anything.
ALERT_MIN_OUTFLOW = env.float("ALERT_MIN_OUTFLOW", default=100000)  # rupees
ALERT_MIN_IMBALANCE = env.float("ALERT_MIN_IMBALANCE", default=0.25)  # share of credits + debits
ALERT_HIGH_OUTFLOW = env.float("ALERT_HIGH_OUTFLOW", default=500000)  # high needs both highs
ALERT_HIGH_IMBALANCE = env.float("ALERT_HIGH_IMBALANCE", default=0.40)
ALERT_MIN_NETWORK_COVERAGE = env.float("ALERT_MIN_NETWORK_COVERAGE", default=0.5)  # share of days
ALERT_NEW_ACCOUNTS = env.int("ALERT_NEW_ACCOUNTS", default=10)  # new accounts, no activity
PMJDY_MIN_INTERVAL_SECONDS = env.int("PMJDY_MIN_INTERVAL_SECONDS", default=3300)

# Automation for the bank-flow / estimate pipeline (run by `watch_transactions` on the dedicated
# worker-bankflow service, which opts in through WATCH_JOBS -- a plain worker never runs these).
PMJDY_INTERVAL_SECONDS = env.int("PMJDY_INTERVAL_SECONDS", default=3600)  # hourly
ESTIMATE_INTERVAL_SECONDS = env.int("ESTIMATE_INTERVAL_SECONDS", default=3600)  # hourly
BANKFLOW_STATEMENT_INTERVAL_SECONDS = env.int("BANKFLOW_STATEMENT_INTERVAL_SECONDS", default=3600)
BANKFLOW_BUILD_FLOWS_INTERVAL_SECONDS = env.int(
    "BANKFLOW_BUILD_FLOWS_INTERVAL_SECONDS", default=86400
)  # re-attribute when new CSPs appear in the sheet
HYPOTHESIS_INTERVAL_SECONDS = env.int("HYPOTHESIS_INTERVAL_SECONDS", default=86400)
# How often the worker checks whether the data changed and the cached dashboard figures need
# re-computing (a no-op when nothing changed).
WARM_CACHE_INTERVAL_SECONDS = env.int("WARM_CACHE_INTERVAL_SECONDS", default=60)
BASELINE_SNAPSHOT_AT = env("BASELINE_SNAPSHOT_AT", default="06:00")  # IST, once a day
BANKFLOW_STATEMENT_PATTERN = env("BANKFLOW_STATEMENT_PATTERN", default="statement_1556_*")
BANKFLOW_MAX_FILE_MB = env.int("BANKFLOW_MAX_FILE_MB", default=100)

# Root of the ingestion file-safety workflow (PRD §7.2, Phase 4/10):
#   <dir>/incoming/  — the watched folder itself
#   <dir>/processed/ — successfully ingested files are moved here
#   <dir>/failed/    — malformed/rejected files are moved here, never deleted
TRANSACTION_DATA_DIR = env("TRANSACTION_DATA_DIR", default=str(BASE_DIR / "data"))

# Nemotron (NVIDIA's OpenAI-compatible endpoint) — autopilot app. Blank key
# means every autopilot generation function degrades to "produced nothing
# this run" (see autopilot/nemotron_client.py) rather than failing loudly —
# unlike GOOGLE_SERVICE_ACCOUNT_FILE/CALLING_SHEET_ID above, this integration
# is additive narration, not something the core pipeline depends on.
NEMOTRON_API_KEY = env("NEMOTRON_API_KEY", default="")
NEMOTRON_MODEL = env("NEMOTRON_MODEL", default="nvidia/nemotron-3-super-120b-a12b")

# SBI pool-account statement flow validation (bankflow app, docs/BANK-FLOW.md).
# Analysis-only: nothing in bankflow writes DailyBalance or switches the balance
# source. The watch folder is only ever READ; files are never moved or deleted.
BANKFLOW_WATCH_DIR = env(
    "BANKFLOW_WATCH_DIR", default="/home/adarsh/login_automation/yonosbi-automation/downloads"
)
# Only statements of this pool account (last 5 digits) are accepted; others are quarantined.
BANKFLOW_EXPECTED_ACCOUNT_SUFFIX = env("BANKFLOW_EXPECTED_ACCOUNT_SUFFIX", default="71556")
# Trailing-character KO variants (e.g. 1A8502957 -> 1A850295) are UNCONFIRMED. Leave False
# until the pattern is confirmed by authoritative documentation or validated examples.
BANKFLOW_CONFIRM_TRAILING_VARIANTS = env.bool("BANKFLOW_CONFIRM_TRAILING_VARIANTS", default=False)
# The flag above is IGNORED unless this also names where the rule was confirmed
# (a document / ticket reference). Prevents an accidental env flip enabling guesses.
BANKFLOW_VARIANT_RULE_CONFIRMATION_REF = env("BANKFLOW_VARIANT_RULE_CONFIRMATION_REF", default="")

# API auth is per-consumer, DB-backed (api.models.ApiConsumer/ApiKey) as of
# Phase 8 — see api/auth.py. Issue keys with `manage.py create_api_key`.
# There is no env-var key list anymore.

# API rate limiting (scalability foundation, 2026-09-21) — see api/v1/router.py.
API_AUTH_THROTTLE_RATE = env("API_AUTH_THROTTLE_RATE", default="120/m")
API_ANON_THROTTLE_RATE = env("API_ANON_THROTTLE_RATE", default="20/m")

# Request-size bound (P0 production-hardening fast-pass, 2026-09-21) — made
# explicit rather than left at Django's own default; no /api/v1 endpoint
# accepts a file/body anywhere near this size (transaction files arrive via
# the watched incoming/ folder, never an HTTP upload to this app).
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=2_621_440)  # 2.5MB

# --- Logging (Phase 12) ------------------------------------------------------
# Human-readable console here; production.py switches to JSON. See
# config/logging.py for the full "never log a secret" processor chain.
LOGGING = build_logging_config(json_output=False)
