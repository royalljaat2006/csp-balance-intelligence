from django.apps import AppConfig


class ApiConfig(AppConfig):
    """django-ninja API layer (PRD §9). No models — reads csp.models."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "api"
