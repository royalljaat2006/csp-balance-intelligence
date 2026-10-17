"""
Shell-wide context — every dashboard page needs the same few things (how fresh the
data is, which environment this is, the navigation) without
each view repeating the same lookup. A context processor, not per-view
boilerplate, because these are properties of the *shell* (base.html), not
of any one page's own data.

Deliberately excluded from `/api/v1/*` and `/admin/` (this processor is
only registered for the dashboard's own request path in settings) — the
API has its own request/response contract and Django admin owns its own
chrome; neither needs "which dashboard theme is active."
"""

from __future__ import annotations

from csp.services import get_data_freshness
from django.conf import settings

from . import navigation, shell


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

    context: dict[str, object] = {
        "env_badge": _env_badge(),
        # Prefix-aware links for embeds that point at routes outside /dashboard/ (Django admin,
        # API docs): under a subpath deployment (URL_PREFIX / FORCE_SCRIPT_NAME) a hardcoded
        # "/admin/" would resolve to a different app's Nginx location on the shared host.
        "url_prefix": getattr(settings, "URL_PREFIX", ""),
    }
    # The shell (sidebar/topbar) only renders for a logged-in user (see
    # base.html) — skip the freshness query entirely for the anonymous
    # login page, which has no shell to show it in.
    if getattr(request, "user", None) and request.user.is_authenticated:
        context["freshness"] = get_data_freshness()
        match = getattr(request, "resolver_match", None)
        context.update(navigation.build(match.url_name if match else None, request.user.is_staff))
        name = match.url_name if match else None
        # Peek only: a missing value is fetched by the page after it has rendered (shell.py).
        have_count, count = shell.peek_attention_count()
        context["needs_attention_count"] = count
        pending = not have_count
        context["day_compare"] = None
        context["day_compare_pending"] = False
        if name in navigation.COMPARE_PAGES:
            have_strip, strip = shell.peek_day_compare()
            context["day_compare"] = strip if have_strip else None
            context["day_compare_pending"] = not have_strip
            pending = pending or not have_strip
        context["shell_lazy"] = pending
    return context
