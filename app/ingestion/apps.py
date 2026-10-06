from django.apps import AppConfig


class IngestionConfig(AppConfig):
    """
    CALLING SHEET + TRANSACTION-file ingestion (PRD §7-§8, FR1-FR3).

    Registered in INSTALLED_APPS so its management commands are discoverable
    by `manage.py`. No models of its own — writes into csp.models.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "ingestion"
