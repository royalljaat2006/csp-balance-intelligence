# Security — CSP Average Balance Tracker

## Reporting a vulnerability

Email the maintainer listed in `docs/DEPLOYMENT.md`'s admin handoff section. Do not open a public
issue for a suspected vulnerability. Include: what you found, how to reproduce it, and the
potential impact. Expect an acknowledgement within 2 business days.

## What this project already does

- **Auth**: API — per-consumer, hashed (SHA-256) API keys (`api/auth.py`), never stored/logged in
  plaintext, scope-checked per endpoint. Dashboard — Django session auth, staff-gated for
  operational pages (`login_required` + `is_staff`).
- **Secrets**: never committed (`.env` gitignored, `.env.example` has placeholders only). Google
  service-account key lives outside the repo, bind-mounted read-only. `config.settings.production`
  refuses to start on a placeholder `DJANGO_SECRET_KEY`.
- **Logging**: `api/middleware.py` never logs headers/query params/body; a `_NEVER_LOG_HEADERS`
  set (`x-api-key`, `authorization`, `cookie`) makes this explicit. No password, token, or account
  number is ever logged anywhere in the codebase (verified: `from_account`/`to_account` are never
  even rendered in any template or API response today).
- **Rate limiting**: per-API-key and per-IP throttles (`api/v1/router.py`, `ninja.throttling`,
  Redis-backed in production).
- **Input bounds**: response-size caps (`api/pagination.py`, max 500/page), date-range caps (366
  days), request-size limit (`DATA_UPLOAD_MAX_MEMORY_SIZE`).
- **CSRF**: Django's CSRF middleware on every session-authenticated view; the API is header-key
  authenticated (not cookie-based), so CSRF doesn't apply to it, by design.
- **CORS**: intentionally not enabled — every API consumer is server-to-server with `X-API-Key`;
  see `config/settings/base.py`'s comment for why adding it blindly would be a regression.
- **Transport**: `SECURE_PROXY_SSL_HEADER` set for the real Nginx-terminated-TLS deployment;
  `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` on in production.
- **Dependency scanning**: `pip-audit` run against the locked dependency set — see "Known issues"
  below for the current result.

## Known issues (tracked, not hidden)

- **Django 5.1.15 has 7 published CVEs** (PYSEC-2026-198/199/201/2090/2091/2092/3717) with fixes
  only in the 5.2.15+/6.0.6+ lines, not backported to 5.1. Upgrading is a deliberate, separately
  tested change (a Django minor/major bump can break ORM/admin/form behavior) — not done as part
  of routine hardening. Track and schedule this upgrade explicitly.
- **HSTS / forced HTTPS redirect** are not yet enabled — deliberately, pending a real domain + TLS
  certificate on the production host (see `docs/DEPLOYMENT.md`). Enable
  `SECURE_HSTS_SECONDS`/`SECURE_SSL_REDIRECT` once those exist; enabling HSTS before TLS is
  confirmed stable is a real, hard-to-reverse footgun.
- **`DJANGO_SECRET_KEY`/`X_FRAME_OPTIONS`**: `manage.py check --deploy` flags the local dev
  placeholder secret key (expected — never a real production value in this repo) and
  `X_FRAME_OPTIONS=SAMEORIGIN` (deliberate — the dashboard embeds its own `/api/v1/docs`).

## Reviewed and found not applicable

- **SSRF**: no user-supplied URL is ever fetched server-side (Nemotron/Sheets/Telegram endpoints
  are all fixed, config-driven, not request-controlled).
- **File upload**: transaction files arrive via a watched filesystem folder or Telegram, never an
  HTTP upload endpoint — no multipart/file-upload attack surface exists in this app.
- **SQL injection**: all queries go through the Django ORM or parameterized raw SQL
  (`csp/services.py`'s `_query_daily_activity`, `sync_monthly_summary`'s mart query) — no
  string-interpolated SQL anywhere.
