"""manage.py generate_insights — one Nemotron-written daily briefing over
real, already-computed network numbers (see autopilot/services.py)."""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand

from autopilot.services import generate_daily_briefing

logger = structlog.get_logger("autopilot")


class Command(BaseCommand):
    help = "Generate today's Nemotron narrative briefing (Insight)."

    def add_arguments(self, parser):
        parser.add_argument("--month", default=None, help="YYYY-MM (defaults to current month).")

    def handle(self, *args, **options):
        insight = generate_daily_briefing(month=options["month"])
        if insight is None:
            self.stdout.write(self.style.WARNING("No insight generated (see logs for why)."))
            return
        self.stdout.write(self.style.SUCCESS(f"Insight #{insight.id}: {insight.headline}"))
