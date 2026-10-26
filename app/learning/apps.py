from django.apps import AppConfig


class LearningConfig(AppConfig):
    """The learning loop around the shadow Avg Balance estimate: frozen predictions, the SBI value
    that later proved them right or wrong, human feedback, candidate-method comparison and the
    audited promotion of a method. Reads bankflow and csp; never writes DailyBalance, Csp, or any
    Calling Sheet table, and never changes how an estimate is computed."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "learning"
