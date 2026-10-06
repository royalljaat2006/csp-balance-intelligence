"""Admin for API consumers/keys. `key_hash` is deliberately never shown —
identify a key by its prefix/label, not its hash."""

from django.contrib import admin

from .models import ApiConsumer, ApiKey


class ApiKeyInline(admin.TabularInline):
    model = ApiKey
    extra = 0
    fields = (
        "label",
        "key_prefix",
        "scopes",
        "is_active",
        "created_at",
        "last_used_at",
        "revoked_at",
    )
    readonly_fields = ("key_prefix", "created_at", "last_used_at")


@admin.register(ApiConsumer)
class ApiConsumerAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    search_fields = ("name",)
    inlines = [ApiKeyInline]


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("consumer", "label", "key_prefix", "scopes", "is_active", "last_used_at")
    list_filter = ("is_active", "consumer")
    search_fields = ("consumer__name", "label", "key_prefix")
    readonly_fields = ("key_prefix", "key_hash", "created_at", "last_used_at")

    def has_add_permission(self, request):
        # Keys are minted via `manage.py create_api_key` so the raw value can
        # be shown exactly once on a terminal, never persisted to a request
        # log or an admin form submission.
        return False
