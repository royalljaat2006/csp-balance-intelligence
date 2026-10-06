"""Health/liveness endpoint — PRD §8 FR7, R730 standard "Health checks and readiness"."""

from csp.services import get_data_freshness
from django.db import connection
from django.http import HttpResponse, JsonResponse

from common import metrics


def health(request):
    """
    GET /health — kept as-is for backward compatibility (existing Docker
    healthchecks/monitoring already point at this path); identical to
    /health/ready below. New integrations should use /health/live or
    /health/ready explicitly instead — see those for why the split exists
    (P0 production-hardening fast-pass, 2026-09-21).
    """
    return health_ready(request)


def health_live(request):
    """
    GET /health/live — liveness only: "is this process able to respond at
    all." Deliberately does NOT check the database — a liveness probe that
    fails because a dependency is briefly down causes an orchestrator to
    kill/restart a perfectly healthy process, which only makes a DB blip
    worse (thundering herd of restarts). No auth, no expensive work.
    """
    return JsonResponse({"status": "ok"})


def health_ready(request):
    """
    GET /health/ready — readiness: "is this process able to serve real
    traffic right now." Checks DB connectivity and reports how stale the
    CALLING SHEET ingest is, via csp.services.get_data_freshness() — the
    same canonical freshness check the dashboard's own freshness indicator
    reads, so the two surfaces can never disagree about what "fresh"
    means. Does not itself call out to Google Sheets, Telegram, or the
    filesystem. A load balancer should stop routing to a replica that
    fails this, without killing the process (that's liveness's job).
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_ok = False

    freshness = get_data_freshness()

    status_code = 200 if db_ok else 503
    return JsonResponse(
        {
            "status": "ok" if db_ok else "db_unreachable",
            "db_ok": db_ok,
            "calling_sheet_last_success": freshness["calling_sheet_last_success"],
            "calling_sheet_stale": freshness["calling_sheet_stale"],
        },
        status=status_code,
    )


def metrics_view(request):
    """
    GET /metrics — Prometheus text-exposition format, no auth (same
    reasoning as /health: numbers only, no secrets, cheap to compute). Kept
    unauthenticated so a standard Prometheus scrape config needs no
    credentials wiring; if this ever needs to be private, gate it at the
    Nginx layer (see docs/DEPLOYMENT.md) rather than adding app-level auth
    that a scraper would then need to carry.
    """
    return HttpResponse(metrics.render_prometheus_text(), content_type="text/plain; version=0.0.4")
