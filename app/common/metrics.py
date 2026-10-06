"""
In-process metrics registry + Prometheus text-exposition rendering
(scalability foundation, 2026-09-21).

Deliberately dependency-free (no `prometheus_client`): a handful of
thread-safe counters/histograms plus a couple of on-scrape gauges (DB
connection count, RQ queue depth) is all `/metrics` needs, and hand-rolling
it avoids pulling in a library whose multiprocess mode (required once
gunicorn runs >1 worker) needs its own shared-directory configuration this
project doesn't have set up yet — see the scalability report's
"observability" section for that follow-up.

IMPORTANT LIMITATION: these counters live in one process's memory. With
gunicorn `--workers 3` (or multiple container replicas), `/metrics` on any
one process only reflects that process's own traffic — real per-replica
aggregation needs each replica scraped separately (or a switch to
prometheus_client's multiprocess mode) once this goes past a single
instance. Documented, not hidden.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()

_http_requests_total: dict[tuple[str, str, str], int] = defaultdict(int)
_http_request_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
_http_request_duration_count: dict[tuple[str, str], int] = defaultdict(int)
# Fixed histogram buckets, seconds — matches typical Prometheus latency SLOs.
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_http_request_duration_bucket: dict[tuple[str, str, float], int] = defaultdict(int)

_cache_hits = 0
_cache_misses = 0
_cache_errors = 0

_external_api_calls_total: dict[tuple[str, str], int] = defaultdict(int)
_external_api_duration_sum: dict[str, float] = defaultdict(float)
_external_api_duration_count: dict[str, int] = defaultdict(int)

# job outcome: "ok" | "failed" | "skipped_locked" — see watch_transactions.py
_job_runs_total: dict[tuple[str, str], int] = defaultdict(int)

_PROCESS_STARTED_AT = time.time()


def record_http_request(*, method: str, route: str, status: int, duration_seconds: float) -> None:
    with _lock:
        _http_requests_total[(method, route, str(status))] += 1
        _http_request_duration_sum[(method, route)] += duration_seconds
        _http_request_duration_count[(method, route)] += 1
        for bucket in _LATENCY_BUCKETS:
            if duration_seconds <= bucket:
                _http_request_duration_bucket[(method, route, bucket)] += 1


def record_cache_hit() -> None:
    global _cache_hits
    with _lock:
        _cache_hits += 1


def record_cache_miss() -> None:
    global _cache_misses
    with _lock:
        _cache_misses += 1


def record_cache_error() -> None:
    global _cache_errors
    with _lock:
        _cache_errors += 1


def record_job_run(*, job: str, outcome: str) -> None:
    with _lock:
        _job_runs_total[(job, outcome)] += 1


def record_external_api_call(*, service: str, outcome: str, duration_seconds: float) -> None:
    """`service` e.g. "nemotron"/"telegram"/"google_sheets"; `outcome` e.g.
    "ok"/"error"/"timeout"."""
    with _lock:
        _external_api_calls_total[(service, outcome)] += 1
        _external_api_duration_sum[service] += duration_seconds
        _external_api_duration_count[service] += 1


def _queue_depths() -> dict[str, int]:
    """RQ queue depth per queue, if Redis/rq are reachable — never raises."""
    try:
        import redis as redis_lib
        from django.conf import settings
        from rq import Queue

        if not settings.REDIS_URL:
            return {}
        conn = redis_lib.from_url(
            settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return {name: Queue(name, connection=conn).count for name in ("agents",)}
    except Exception:  # noqa: BLE001 — a metrics scrape must never 500 the endpoint
        return {}


def _db_connection_count() -> int | None:
    """Current backend connection count for the default DB, via
    pg_stat_activity. Postgres-only (mirrors the rest of the app's
    Postgres-only raw-SQL spots, e.g. csp/services.py's daily_activity
    reader) — returns None on SQLite (dev/test) rather than faking a number."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
            row = cursor.fetchone()
            return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 — never let a metrics scrape break on a transient DB hiccup
        return None


def render_prometheus_text() -> str:
    lines: list[str] = []

    lines.append("# HELP process_uptime_seconds Seconds since this process started.")
    lines.append("# TYPE process_uptime_seconds gauge")
    lines.append(f"process_uptime_seconds {time.time() - _PROCESS_STARTED_AT:.3f}")

    with _lock:
        lines.append("# HELP http_requests_total Total HTTP requests handled by this process.")
        lines.append("# TYPE http_requests_total counter")
        for (method, route, status), count in sorted(_http_requests_total.items()):
            labels = f'method="{method}",route="{route}",status="{status}"'
            lines.append(f"http_requests_total{{{labels}}} {count}")

        lines.append(
            "# HELP http_request_duration_seconds Request latency histogram (this process only)."
        )
        lines.append("# TYPE http_request_duration_seconds histogram")
        for (method, route), total in sorted(_http_request_duration_sum.items()):
            count = _http_request_duration_count[(method, route)]
            base_labels = f'method="{method}",route="{route}"'
            cumulative = 0
            for bucket in _LATENCY_BUCKETS:
                key = (method, route, bucket)
                cumulative = _http_request_duration_bucket.get(key, cumulative)
                metric = "http_request_duration_seconds_bucket"
                lines.append(f'{metric}{{{base_labels},le="{bucket}"}} {cumulative}')
            lines.append(
                f'http_request_duration_seconds_bucket{{{base_labels},le="+Inf"}} {count}'
            )
            lines.append(f"http_request_duration_seconds_sum{{{base_labels}}} {total:.6f}")
            lines.append(f"http_request_duration_seconds_count{{{base_labels}}} {count}")

        lines.append(
            "# HELP job_runs_total Scheduled worker job outcomes (ok/failed/skipped_locked)."
        )
        lines.append("# TYPE job_runs_total counter")
        for (job, outcome), count in sorted(_job_runs_total.items()):
            lines.append(f'job_runs_total{{job="{job}",outcome="{outcome}"}} {count}')

        lines.append("# HELP cache_operations_total Cache-aside hit/miss/error counts.")
        lines.append("# TYPE cache_operations_total counter")
        lines.append(f'cache_operations_total{{result="hit"}} {_cache_hits}')
        lines.append(f'cache_operations_total{{result="miss"}} {_cache_misses}')
        lines.append(f'cache_operations_total{{result="error"}} {_cache_errors}')

        lines.append("# HELP external_api_calls_total External API calls by service/outcome.")
        lines.append("# TYPE external_api_calls_total counter")
        for (service, outcome), count in sorted(_external_api_calls_total.items()):
            lines.append(
                f'external_api_calls_total{{service="{service}",outcome="{outcome}"}} {count}'
            )
        lines.append(
            "# HELP external_api_duration_seconds_sum Total time spent in external API calls."
        )
        lines.append("# TYPE external_api_duration_seconds_sum counter")
        for service, total in sorted(_external_api_duration_sum.items()):
            lines.append(f'external_api_duration_seconds_sum{{service="{service}"}} {total:.6f}')

    db_connections = _db_connection_count()
    if db_connections is not None:
        lines.append("# HELP db_connections_current Current Postgres backend connections.")
        lines.append("# TYPE db_connections_current gauge")
        lines.append(f"db_connections_current {db_connections}")

    lines.append("# HELP queue_depth_jobs Pending job count per RQ queue.")
    lines.append("# TYPE queue_depth_jobs gauge")
    for queue_name, depth in sorted(_queue_depths().items()):
        lines.append(f'queue_depth_jobs{{queue="{queue_name}"}} {depth}')

    return "\n".join(lines) + "\n"
