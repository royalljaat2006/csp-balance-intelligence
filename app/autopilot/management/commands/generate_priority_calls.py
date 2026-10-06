"""manage.py generate_priority_calls — Nemotron-ranked call-priority list
for the current month (see autopilot/services.py). Informational only."""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand

from autopilot.services import generate_priority_calls

logger = structlog.get_logger("autopilot")


class Command(BaseCommand):
    help = "Regenerate this month's Nemotron-ranked priority-call list."

    def add_arguments(self, parser):
        parser.add_argument("--month", default=None, help="YYYY-MM (defaults to current month).")
        parser.add_argument("--limit", type=int, default=15)

    def handle(self, *args, **options):
        calls = generate_priority_calls(month=options["month"], limit=options["limit"])
        if not calls:
            self.stdout.write(self.style.WARNING("No priority calls generated (see logs for why)."))
            return
        self.stdout.write(self.style.SUCCESS(f"Generated {len(calls)} priority call(s)."))
