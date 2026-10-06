from django.apps import AppConfig


class CspConfig(AppConfig):
    """Core domain models — Csp, DailyBalance, Transaction, MonthlySummary, IngestLog (PRD §10)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "csp"
