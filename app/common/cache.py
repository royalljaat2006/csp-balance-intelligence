"""
Redis-backed cache-aside + distributed-lock helpers (scalability foundation,
2026-09-21). Two rules, non-negotiable:

1. Redis is a PERFORMANCE optimization only, never a source of truth. Every
   helper here fails OPEN — if the cache backend is unreachable, slow, or
   misconfigured, callers get exactly what they'd get with no cache at all
   (a real DB read, or "the lock wasn't acquired but proceed anyway"),
   never a crash and never a fabricated value. See `docs/DEPLOYMENT.md` /
   the scalability report for the "Redis unavailable" failure-handling
   requirement this satisfies.
2. Nothing that feeds a business decision (agent findings, approval flow,
   balance/slab figures a human could act on) is cached through this
   module. Only network-wide *dashboard display* rollups are — see the
   call sites in dashboard/views.py. csp/services.py itself is never
   modified to cache anything, so autopilot/agent_tools.py and the API
   always read live from PostgreSQL, unchanged from before this pass.

Cache keys are namespaced by a "generation" counter instead of relying on
Redis pattern-delete (which the LocMemCache dev/test fallback doesn't
support at all): ingestion completing bumps the generation, which
instantly invalidates every cache_aside() key from the previous
generation without needing to know their names in advance.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from django.core.cache import cache

from common import metrics

logger = structlog.get_logger("common.cache")

_GENERATION_KEY = "csp-tracker:cache-generation"
_LOCK_TIMEOUT_FLOOR_SECONDS = 5
# Stampede guard (P1 fast-pass, 2026-09-21): how long one waiter gives the
# lock-holder to finish computing before giving up and computing itself
# anyway. Short and bounded on purpose — a slow recompute must never make
# every OTHER request pile up waiting; worst case here is a few duplicate
# computations, never a growing queue.
_STAMPEDE_WAIT_ATTEMPTS = 3
_STAMPEDE_WAIT_SECONDS = 0.05


def _safe[T](op: str, fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — any cache backend failure degrades, never crashes
        metrics.record_cache_error()
        logger.warning("cache_backend_error", op=op, error_type=type(exc).__name__, error=str(exc))
        return default


def get_generation() -> int:
    """Current cache generation. Missing/unreadable cache -> generation 0,
    which is a perfectly valid (if perpetually "unbumped") generation."""
    return _safe("get_generation", lambda: cache.get(_GENERATION_KEY, 0) or 0, 0)


def _bump() -> None:
    if cache.get(_GENERATION_KEY) is not None:
        cache.incr(_GENERATION_KEY)
    else:
        cache.set(_GENERATION_KEY, 1, timeout=None)


def bump_generation() -> None:
    """Invalidates every cache_aside() entry from prior generations.
    Called by ingestion/snapshot commands the moment new data actually
    lands — see ingestion/file_ingest.py, calling_sheet_ingest.py,
    csp/management/commands/sync_daily_snapshots.py."""
    _safe("bump_generation", _bump, None)


def cache_aside[T](key: str, *, ttl: int, fn: Callable[[], T]) -> T:
    """Read-through cache for one dashboard aggregate. `fn` is the exact
    same canonical csp.services call the uncached code path already made —
    this wraps it, never replaces or reimplements it. TTL is a ceiling, not
    the only invalidation path: bump_generation() (called right after real
    data lands) invalidates immediately, so a page load right after
    ingestion never shows a generation-old number even mid-TTL.

    Stampede guard: on a miss, only one caller actually runs `fn()` — the
    rest briefly wait for its result instead of all recomputing the same
    expensive query at once (see try_acquire_lock's fail-open reasoning;
    same rule here — a broken lock backend just means no de-duplication,
    never a stuck request)."""
    namespaced_key = f"csp-tracker:v{get_generation()}:{key}"
    cached = _safe("get", lambda: cache.get(namespaced_key), None)
    if cached is not None:
        metrics.record_cache_hit()
        return cached
    metrics.record_cache_miss()

    if try_acquire_lock(f"compute:{namespaced_key}", ttl=max(ttl, _LOCK_TIMEOUT_FLOOR_SECONDS)):
        try:
            value = fn()
            _safe("set", lambda: cache.set(namespaced_key, value, timeout=ttl), None)
            return value
        finally:
            release_lock(f"compute:{namespaced_key}")

    # Someone else is already computing this key — wait briefly for their
    # result instead of recomputing immediately.
    for _ in range(_STAMPEDE_WAIT_ATTEMPTS):
        time.sleep(_STAMPEDE_WAIT_SECONDS)
        cached = _safe("get", lambda: cache.get(namespaced_key), None)
        if cached is not None:
            metrics.record_cache_hit()
            return cached
    return fn()


def try_acquire_lock(key: str, *, ttl: int) -> bool:
    """Distributed mutex so N horizontally-scaled replicas of the *same*
    worker role never process the same tick twice. Fails OPEN (returns True
    -> caller proceeds) if the cache backend itself is unavailable: every
    job this guards (ingest_calling_sheet, ingest_transactions,
    sync_daily_snapshots, poll_telegram, run_autopilot) is independently
    idempotent (see their own docstrings), so "possibly ran twice because
    Redis was briefly down" is safe, while "possibly never ran because
    Redis was briefly down" is not — fail open is the correct direction
    here, unlike a financial mutation lock where fail-closed would be."""
    lock_key = f"csp-tracker:lock:{key}"
    ttl = max(ttl, _LOCK_TIMEOUT_FLOOR_SECONDS)
    return _safe("acquire_lock", lambda: cache.add(lock_key, time.time(), timeout=ttl), True)


def release_lock(key: str) -> None:
    """Releases a lock as soon as the guarded job finishes, rather than
    waiting out its TTL — the TTL is only a crash safety net (a worker
    that dies mid-job still frees the lock eventually), not the normal
    release path. Safe/no-op if the backend is unavailable or the key is
    already gone."""
    lock_key = f"csp-tracker:lock:{key}"
    _safe("release_lock", lambda: cache.delete(lock_key), None)


def is_locked(key: str) -> bool:
    """Read-only peek at whether a job's lock is currently held — i.e. is
    that job actually executing right now, somewhere in the fleet. Used by
    the live pipeline view (dashboard/pipeline_state.py) to report a REAL
    "running" state instead of inferring one; never acquires or releases
    anything itself. Returns False (not "unknown") if the cache backend is
    unavailable — a live view that can't reach Redis should show idle/last-
    known-state, never a fabricated "running" indicator."""
    lock_key = f"csp-tracker:lock:{key}"
    return _safe("peek_lock", lambda: cache.get(lock_key) is not None, False)


# --- Live job progress (Phase: real-time agentic pipeline, 2026-10-07) -----
# Separate from the lock mechanism above: a lock says a job is running, this
# says how far through it is. Only a long-running, large-file job (currently
# transaction ingestion) ever writes one — most jobs here finish in well
# under a second and have nothing meaningful to report mid-flight, so the
# live pipeline view correctly shows no progress bar for them rather than a
# fabricated one. A short TTL (not "cleared on completion" alone) is the
# real safety net: a worker that dies mid-job leaves no lingering stale
# progress for longer than the TTL, even if its own cleanup never runs.
_PROGRESS_TTL_SECONDS = 120


def set_job_progress(key: str, *, processed: int, total: int | None) -> None:
    """`total` is the real row count the source file itself reports (e.g.
    openpyxl's sheet.max_row) — never estimated or guessed. None when the
    caller genuinely doesn't know a total yet (never 0 standing in for
    "unknown")."""
    progress_key = f"csp-tracker:progress:{key}"
    value = {"processed": processed, "total": total}
    _safe(
        "set_progress",
        lambda: cache.set(progress_key, value, timeout=_PROGRESS_TTL_SECONDS),
        None,
    )


def get_job_progress(key: str) -> dict | None:
    """None (not a fabricated 0/0) when no progress has been reported, the
    entry expired, or the cache backend is unreachable — the pipeline view
    must render "no progress data" rather than a fake empty bar in any of
    those cases."""
    progress_key = f"csp-tracker:progress:{key}"
    return _safe("get_progress", lambda: cache.get(progress_key), None)


def clear_job_progress(key: str) -> None:
    """Called as soon as the job it describes actually finishes — the TTL
    above is only the crash safety net, not the normal clear path (same
    relationship release_lock has to a lock's TTL)."""
    progress_key = f"csp-tracker:progress:{key}"
    _safe("clear_progress", lambda: cache.delete(progress_key), None)
