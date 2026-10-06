from django.apps import AppConfig


class AutopilotConfig(AppConfig):
    """Nemotron-backed narration/prioritization — Insight, PriorityCall,
    AnomalyFlag, DraftMessage. Read/draft only: nothing here sends a
    message or writes to a CSP-facing system on its own (see DraftMessage's
    docstring)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "autopilot"
