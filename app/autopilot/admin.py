"""
Admin console for autopilot output. DraftMessage's approve_drafts/
reject_drafts actions are the one deliberate human gate this app has —
see models.DraftMessage's docstring for why nothing sends itself.
"""

from django.contrib import admin
from django.utils import timezone

from .models import AnomalyFlag, DraftMessage, Insight, PriorityCall


@admin.register(Insight)
class InsightAdmin(admin.ModelAdmin):
    list_display = ("month", "headline", "model_used", "generated_at")
    list_filter = ("month",)
    search_fields = ("headline", "body")
    readonly_fields = [f.name for f in Insight._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(PriorityCall)
class PriorityCallAdmin(admin.ModelAdmin):
    list_display = ("month", "rank", "csp", "reason", "generated_at")
    list_filter = ("month",)
    search_fields = ("csp__csp_code", "csp__name", "reason")
    readonly_fields = [f.name for f in PriorityCall._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(AnomalyFlag)
class AnomalyFlagAdmin(admin.ModelAdmin):
    list_display = ("severity", "description", "ingest_log", "generated_at", "reviewed_at")
    list_filter = ("severity", "ingest_log__source")
    search_fields = ("description",)
    readonly_fields = ("ingest_log", "severity", "description", "raw_context", "generated_at")
    actions = ["mark_reviewed"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark selected flags as reviewed")
    def mark_reviewed(self, request, queryset):
        updated = queryset.filter(reviewed_at__isnull=True).update(
            reviewed_at=timezone.now(), reviewed_by=request.user
        )
        self.message_user(request, f"{updated} anomaly flag(s) marked reviewed.")


@admin.register(DraftMessage)
class DraftMessageAdmin(admin.ModelAdmin):
    list_display = ("csp", "channel", "status", "generated_at", "reviewed_at", "reviewed_by")
    list_filter = ("status", "channel")
    search_fields = ("csp__csp_code", "csp__name", "text")
    readonly_fields = ("csp", "channel", "text", "generated_at")
    actions = ["approve_drafts", "reject_drafts"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Approve selected drafts (still requires manual sending)")
    def approve_drafts(self, request, queryset):
        updated = queryset.filter(status=DraftMessage.Status.DRAFT).update(
            status=DraftMessage.Status.APPROVED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(
            request,
            f"{updated} draft(s) approved. This app has no messaging-provider integration "
            "yet — sending is still a manual step.",
        )

    @admin.action(description="Reject selected drafts")
    def reject_drafts(self, request, queryset):
        updated = queryset.exclude(status=DraftMessage.Status.SENT).update(
            status=DraftMessage.Status.REJECTED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(request, f"{updated} draft(s) rejected.")
