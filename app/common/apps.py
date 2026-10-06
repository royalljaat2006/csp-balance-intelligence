from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Cross-cutting bits that don't belong to a single domain app — /health, shared helpers."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"
