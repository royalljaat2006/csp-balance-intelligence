"""
Primary/read-replica routing scaffold (scalability foundation, 2026-09-21).

Deliberately INERT by default: `db_for_read`/`db_for_write` both return
None, which tells Django "no opinion, use whatever `.using()` says or fall
back to `default`" — so wiring this router into DATABASE_ROUTERS changes
*nothing* about current behaviour. It exists so that:

  - `DATABASES["replica"]` is a real, always-valid alias (see
    config/settings/base.py — it points at DATABASE_REPLICA_URL if set, or
    at the same database as `default` otherwise), so `.using("replica")`
    works today without a second Postgres instance provisioned yet.
  - A future, explicitly-reviewed change can route specific read-heavy,
    staleness-tolerant queries (e.g. a paginated CSP directory listing) at
    "replica" by calling `.using("replica")` at the call site, without
    inventing new settings plumbing at that point.

What this router will NEVER be responsible for: deciding to read stale
data for anything that feeds a business decision (balance figures, slab
classification, agent findings, the approval flow). That stays on
`default` — see the scalability report's Redis/DB design section for why
this is opt-in per read, not a blanket policy.
"""

from __future__ import annotations

from typing import Any


class PrimaryReplicaRouter:
    def db_for_read(self, model: type, **hints: Any) -> str | None:
        return None

    def db_for_write(self, model: type, **hints: Any) -> str | None:
        # Writes never go to "replica" even if a caller mistakenly used
        # .using("replica") for one — every real replica is read-only at
        # the Postgres level anyway, but this makes the app-level intent
        # explicit rather than relying only on that.
        return "default"

    def allow_relation(self, obj1: Any, obj2: Any, **hints: Any) -> bool | None:
        return True

    def allow_migrate(
        self, db: str, app_label: str, model_name: str | None = None, **hints: Any
    ) -> bool | None:
        # Migrations only ever run against "default" — a replica (real or
        # today's same-DB stand-in) is never migrated independently.
        return db == "default"
