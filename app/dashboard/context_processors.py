"""
Shell-wide context — every dashboard page needs the same few things (which
mode is active, how fresh the data is, which environment this is) without
each view repeating the same lookup. A context processor, not per-view
boilerplate, because these are properties of the *shell* (base.html), not
of any one page's own data.

Deliberately excluded from `/api/v1/*` and `/admin/` (this processor is
only registered for the dashboard's own request path in settings) — the
API has its own request/response contract and Django admin owns its own
chrome; neither needs "which dashboard theme is active."
"""

from __future__ import annotations

from autopilot.models import AnomalyFlag
from csp.services import get_data_freshness
from django.conf import settings

MODE_COOKIE = "csp_mode"
DEFAULT_MODE = "tracker"


def _env_badge() -> str:
    """LOCAL/PREVIEW/LIVE — derived from settings.DEBUG + APP_ENV rather than
    trusting APP_ENV alone: a real deploy's .env (APP_ENV=production) is
    still read by a developer's local `runserver` (django-environ loads
    .env regardless of settings module), so APP_ENV by itself would
    mislabel a local dev session as LIVE. DEBUG (set per settings module,
    never per-request) is the trustworthy signal for "is this the real
    production process."""
    if settings.DEBUG:
        return "LOCAL"
    if getattr(settings, "APP_ENV", "") == "production":
        return "LIVE"
    return "PREVIEW"


def dashboard_shell(request):
    # path_info, not path: under a subpath deployment (URL_PREFIX /
    # FORCE_SCRIPT_NAME, see config/settings/base.py), request.path includes
    # the prefix (e.g. "/csp-balance-intelligence/dashboard/...") while
    # path_info is always the prefix-stripped portion Django actually
    # routed on -- the only one guaranteed to start with "/dashboard/"
    # regardless of how the app is mounted.
    if not request.path_info.startswith("/dashboard/"):
        return {}

    mode = request.COOKIES.get(MODE_COOKIE)
    if mode not in ("tracker", "eko"):
        mode = DEFAULT_MODE

    context: dict[str, object] = {
        "dashboard_mode": mode,
        "is_eko_mode": mode == "eko",
        "env_badge": _env_badge(),
        # Prefix-aware links for embeds that point at routes outside
        # /dashboard/ (Django admin, API docs) -- under a subpath deployment
        # (URL_PREFIX / FORCE_SCRIPT_NAME, see config/settings/base.py) a
        # hardcoded "/admin/" resolves to a DIFFERENT app's Nginx location
        # block on the shared R730, not ours. Blank by default (root-mounted
        # deployments), matching every other URL_PREFIX use in this project.
        "url_prefix": getattr(settings, "URL_PREFIX", ""),
    }
    # The shell (sidebar/topbar) only renders for a logged-in user (see
    # base.html) — skip the freshness query entirely for the anonymous
    # login page, which has no shell to show it in.
    if getattr(request, "user", None) and request.user.is_authenticated:
        context["freshness"] = get_data_freshness()
        context["unreviewed_anomaly_count"] = AnomalyFlag.objects.filter(
            reviewed_at__isnull=True
        ).count()
    return context
