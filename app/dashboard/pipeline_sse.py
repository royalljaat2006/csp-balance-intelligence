"""
Live Data Pipeline — SSE transport (dashboard/pipeline_state.py is the
source of truth; this module only pushes it to the browser).

Runs under the existing gunicorn sync workers (no architecture change) by
self-terminating each connection well under gunicorn's --timeout (30s) and
nginx's proxy_read_timeout (30s) — the browser's native EventSource then
reconnects automatically, and every reconnect starts with a full `sync`
event of REAL current state, never a replay. This is the standard
short-lived-SSE-under-sync-WSGI pattern, not a workaround: one open
connection occupies one worker for at most ~20s, then frees it.

Every event is JSON built from dashboard.pipeline_state's real reads.
Nothing here invents a value, a timer, or a status.
"""

from __future__ import annotations

import json
import time

from django.http import StreamingHttpResponse

from . import pipeline_state

_MAX_STREAM_SECONDS = 20
_POLL_INTERVAL_SECONDS = 2


def _sse(event: str, data: dict | list) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _generate(
    *, max_seconds: float = _MAX_STREAM_SECONDS, poll_interval: float = _POLL_INTERVAL_SECONDS
):
    """`max_seconds`/`poll_interval` are parameterized so tests can drive
    this generator without waiting out a real ~20s stream — production
    always uses the module defaults (see stream_pipeline_events)."""
    try:
        snapshot = pipeline_state.get_snapshot()
        timeline = pipeline_state.get_activity_timeline()
        yield _sse("sync", snapshot)
        yield _sse("timeline", timeline)

        seen_node_states = {n["key"]: n for n in snapshot["nodes"]}
        seen_counters = snapshot["counters"]
        seen_operation = snapshot["current_operation"]
        seen_event_ts = timeline[0]["ts"] if timeline else None

        elapsed = 0.0
        while elapsed < max_seconds:
            time.sleep(poll_interval)
            elapsed += poll_interval

            current = pipeline_state.get_snapshot()
            for node in current["nodes"]:
                if seen_node_states.get(node["key"]) != node:
                    yield _sse("node_update", node)
                    seen_node_states[node["key"]] = node

            if current["counters"] != seen_counters:
                yield _sse("counters", current["counters"])
                seen_counters = current["counters"]

            if current["current_operation"] != seen_operation:
                yield _sse("current_operation", current["current_operation"] or {})
                seen_operation = current["current_operation"]

            new_timeline = pipeline_state.get_activity_timeline()
            if new_timeline and new_timeline[0]["ts"] != seen_event_ts:
                fresh = []
                for entry in new_timeline:
                    if entry["ts"] == seen_event_ts:
                        break
                    fresh.append(entry)
                for entry in reversed(fresh):
                    yield _sse("activity", entry)
                seen_event_ts = new_timeline[0]["ts"]

            yield _sse("heartbeat", {"ts": current["generated_at"]})
    except Exception as exc:  # noqa: BLE001 — a broken dependency must degrade the stream, not 500 it
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})


def stream_pipeline_events(request) -> StreamingHttpResponse:
    response = StreamingHttpResponse(_generate(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
