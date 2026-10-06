"""Django admin — the ops console for Eko (FR6). Read-oriented for now."""

from django.contrib import admin

from .models import Csp, DailyBalance, IngestLog, MonthlySummary, Transaction

# Cohesive branding with the dashboard (opened in its own tab from the nav —
# it's a big, separate app, not iframed like /api/v1/docs) rather than
# Django's generic "Django administration".
admin.site.site_header = "CSP Balance Tracker — Admin"
admin.site.site_title = "CSP Balance Tracker Admin"
admin.site.index_title = "Data & ingestion console"


@admin.register(Csp)
class CspAdmin(admin.ModelAdmin):
    list_display = (
        "csp_code",
        "name",
        "email",
        "mobile",
        "account_count",
        "status",
        "last_seen_date",
    )
    search_fields = ("csp_code", "name", "mobile", "email")
    list_filter = ("status", "zone")


@admin.register(DailyBalance)
class DailyBalanceAdmin(admin.ModelAdmin):
    list_display = ("csp", "balance_date", "daily_avg_balance", "source")
    list_filter = ("source", "balance_date")
    search_fields = ("csp__csp_code", "csp__name")
    date_hierarchy = "balance_date"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("ref_number", "csp", "txn_date", "txn_type", "category", "direction", "amount")
    list_filter = ("category", "direction", "txn_type")
    search_fields = ("ref_number", "csp__csp_code")
    date_hierarchy = "txn_date"


@admin.register(MonthlySummary)
class MonthlySummaryAdmin(admin.ModelAdmin):
    list_display = (
        "csp",
        "month",
        "mtd_mab",
        "projected_mab",
        "slab",
        "trend_flag",
        "is_eligible",
    )
    list_filter = ("month", "slab", "trend_flag", "is_eligible")
    search_fields = ("csp__csp_code", "csp__name")


@admin.register(IngestLog)
class IngestLogAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "started_at",
        "finished_at",
        "status",
        "rows_read",
        "rows_upserted",
        "rows_rejected",
    )
    list_filter = ("source", "status")
    readonly_fields = [f.name for f in IngestLog._meta.fields]

    def has_add_permission(self, request):
        return False
