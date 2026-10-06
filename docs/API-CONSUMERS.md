# API consumer registry

Who calls this service, with what key, for what. **Update this before issuing a key** — it is the
only record of which projects break if an endpoint changes (docs/API-VERSIONING.md).

Keys are per consumer, scoped, hashed at rest, and issued with:

```bash
docker compose exec web python manage.py create_api_key <consumer-name> --scopes <a,b,c> --label prod
```

The raw key is printed once. The consumer's `ApiConsumer` row + its `ApiKey` rows are also visible
(minus the hash) in the Django admin under *API → Api consumers*.

## Registered consumers

| Consumer | Owner | Env | API version | Endpoints used | Scopes | Created | Last reviewed | Migration notes | Status |
|---|---|---|---|---|---|---|---|---|---|
| *(none yet)* | | | | | | | | | |

No external consumer has integrated as of 2026-09-12 (PRD §6 lists the *intended* ones below).
Add a row the day a key is issued.

## Expected consumers (from PRD §6 — not yet onboarded)

| Consumer | Likely scopes | Notes |
|---|---|---|
| Eko ops dashboard (external, built by another team) | `csp:read, balance:read, incentive:read, projection:read, report:read, comparison:read` | The primary intended consumer; per-CSP + overview + daily-comparison views |
| Reporting / month-end incentive report | `csp:read, incentive:read, report:read` | Probably read-only, monthly cadence |
| Internal ops tools / automation | case by case | Issue the narrowest scope set that works |

## Rules

- **One key per consumer per environment** (`--label prod` / `staging`). Never share a key across
  two projects — that defeats the point of knowing who's affected by a change.
- **Least privilege:** issue only the scopes the consumer's listed endpoints actually need.
- **Rotation:** create the new key, hand it over, confirm it works, *then*
  `manage.py revoke_api_key <old-prefix>`. Record the date here.
- **Review** each row at least quarterly: is the consumer still live? still using those endpoints?
  `ApiKey.last_used_at` (admin) tells you if a key has gone quiet.
- **Before a breaking change:** every consumer whose "Endpoints used" column includes the affected
  endpoint gets notified and a migration note added here.
