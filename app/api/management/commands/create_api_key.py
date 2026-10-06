"""
manage.py create_api_key — issues a new consumer credential.

The raw key is printed to stdout exactly once and never stored anywhere —
only its hash lands in the database. Copy it immediately; there is no way
to recover it afterwards (create a new key and revoke the old one instead).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from api.auth import KEY_PREFIX_LENGTH, generate_raw_key, hash_key
from api.models import ALL_SCOPES, ApiConsumer, ApiKey


class Command(BaseCommand):
    help = "Create a new API key for a consumer (creating the consumer if it doesn't exist)."

    def add_arguments(self, parser):
        parser.add_argument("consumer_name", help="e.g. 'dashboard', 'reporting-system'")
        parser.add_argument(
            "--scopes",
            required=True,
            help=f"Comma-separated, from: {', '.join(ALL_SCOPES)}",
        )
        parser.add_argument("--label", default="", help="e.g. 'prod', 'staging'")

    def handle(self, *args, **options):
        scopes = [s.strip() for s in options["scopes"].split(",") if s.strip()]
        invalid = [s for s in scopes if s not in ALL_SCOPES]
        if invalid:
            raise CommandError(f"Unknown scope(s): {invalid}. Valid scopes: {ALL_SCOPES}")
        if not scopes:
            raise CommandError("At least one scope is required.")

        consumer, created = ApiConsumer.objects.get_or_create(name=options["consumer_name"])
        if created:
            self.stdout.write(f"Created new consumer: {consumer.name}")

        raw_key = generate_raw_key()
        ApiKey.objects.create(
            consumer=consumer,
            label=options["label"],
            key_prefix=raw_key[:KEY_PREFIX_LENGTH],
            key_hash=hash_key(raw_key),
            scopes=",".join(scopes),
        )

        self.stdout.write(
            self.style.SUCCESS(f"\nAPI key for '{consumer.name}' (scopes: {scopes}):")
        )
        self.stdout.write(raw_key)
        self.stdout.write(
            self.style.WARNING(
                "\nCopy this now — it will not be shown again. Give it to the consumer as "
                "the X-API-Key header value."
            )
        )
