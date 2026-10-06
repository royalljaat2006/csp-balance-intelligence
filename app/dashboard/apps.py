from django.apps import AppConfig


class DashboardConfig(AppConfig):
    """Server-rendered, staff-login-gated web UI over csp.services — the
    human-facing half of the app, alongside the API (Eko ops + CSP field team)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"
