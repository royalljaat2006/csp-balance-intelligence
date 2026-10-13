from django.apps import AppConfig


class BankflowConfig(AppConfig):
    """SBI pool-account statement ingestion + CSP transaction-flow validation.

    Deliberately separate from `csp`: nothing here writes to DailyBalance,
    Csp master data or any Calling Sheet-derived table. See docs/BANK-FLOW.md.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "bankflow"
