"""manage.py revoke_api_key <key_prefix> — disables a key immediately (Phase 8: revoke/disable)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import ApiKey


class Command(BaseCommand):
    help = "Revoke an API key by its prefix (shown when it was created, and in the admin)."

    def add_arguments(self, parser):
        parser.add_argument("key_prefix")

    def handle(self, *args, **options):
        prefix = options["key_prefix"]
        matches = list(ApiKey.objects.filter(key_prefix=prefix, is_active=True))
        if not matches:
            raise CommandError(f"No active key found with prefix {prefix!r}.")
        if len(matches) > 1:
            raise CommandError(f"Prefix {prefix!r} is ambiguous ({len(matches)} matches).")

        key = matches[0]
        key.is_active = False
        key.revoked_at = timezone.now()
        key.save(update_fields=["is_active", "revoked_at"])
        self.stdout.write(
            self.style.SUCCESS(f"Revoked key {prefix} for consumer {key.consumer.name}.")
        )
