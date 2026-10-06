# API versioning

## Rule

Every public business endpoint lives under an explicit version prefix:

```
/api/v1/...
```

There are **no unversioned business endpoints**. The only unversioned routes are infrastructure:
`/health` (liveness, no auth) and `/admin/` (Django admin, session auth, not an API).

## What "v1 is stable" means

Once a consumer is registered against v1 (docs/API-CONSUMERS.md), the following are frozen for v1:

- every path and HTTP method in docs/API-CATALOG.md
- every field name and type in a response schema (`app/api/schemas.py`)
- the envelope shape (`data` / `pagination` / `request_id`) and the error shape (RFC 9457, docs/API-ERRORS.md)
- the meaning of every scope
- the semantics of every query parameter

**Allowed within v1 (non-breaking):**
- adding a new endpoint
- adding a new *optional* query parameter with a default that preserves current behaviour
- adding a new field to a response (consumers must ignore unknown fields)
- adding a new scope
- bug fixes that make a response match its documented contract

**Not allowed within v1 (breaking → needs v2):**
- removing or renaming a path, field, parameter, or scope
- changing a field's type or meaning
- making an optional parameter required
- changing the envelope or error format
- changing what a scope grants

## Introducing v2

1. Create `app/api/v2/` mirroring `app/api/v1/` (its own `router.py` + resource modules). Shared
   code (`auth.py`, `errors.py`, `middleware.py`) stays shared; schemas that differ get their own
   `v2` variants rather than mutating v1's.
2. Mount it in `config/urls.py` as `path("api/v2/", api_v2.urls)` alongside v1.
3. Add every v2 endpoint to docs/API-CATALOG.md with `introduced: v2`.
4. **Do not delete v1.** Mark v1 endpoints `deprecated` in the catalog with a sunset date, and
   notify every consumer listed against them in docs/API-CONSUMERS.md.
5. Remove v1 only after every registered consumer has migrated and the sunset date has passed.

## Pre-launch note (September 2026)

The service was reorganised from bare `/v1/...` to `/api/v1/...`, and the response envelope +
per-consumer auth were introduced, **before any consumer existed** (PRD confirms no dashboard/agent
had integrated). That was a launch of v1, not a breaking change to it — the frozen contract
above starts from this shape.
