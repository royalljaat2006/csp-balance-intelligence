"""manage.py draft_csp_nudges — Nemotron-drafted CSP nudge messages, DRAFT
ONLY (see autopilot/models.py's DraftMessage docstring). Nothing here sends
anything; a human approves drafts in the admin before any manual send."""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand

from autopilot.services import draft_csp_nudges

logger = structlog.get_logger("autopilot")


class Command(BaseCommand):
    help = "Draft nudge messages for CSPs close to a slab threshold (draft only, never sent)."

    def add_arguments(self, parser):
        parser.add_argument("--month", default=None, help="YYYY-MM (defaults to current month).")
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        drafts = draft_csp_nudges(month=options["month"], limit=options["limit"])
        if not drafts:
            self.stdout.write(self.style.WARNING("No drafts generated (see logs for why)."))
            return
        self.stdout.write(
            self.style.SUCCESS(f"Drafted {len(drafts)} message(s) — pending approval in admin.")
        )
