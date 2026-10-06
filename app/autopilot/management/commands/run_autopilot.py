"""manage.py run_autopilot — runs all four Nemotron-backed generation
steps in sequence, each independently guarded so one failing (bad API key,
network blip, malformed response) never blocks the others.

This is the one command ingestion/watch_transactions.py's worker loop
actually calls (see WATCH_AUTOPILOT_INTERVAL_SECONDS) — the four commands
this wraps stay independently runnable for manual use/testing.
"""

from __future__ import annotations

import structlog
from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = structlog.get_logger("autopilot")

_STEPS = [
    "generate_insights",
    "generate_priority_calls",
    "review_ingest_anomalies",
    "draft_csp_nudges",
    "run_csp_operations_agent",
]


class Command(BaseCommand):
    help = "Run every autopilot generation step once, in sequence."

    def handle(self, *args, **options):
        for step in _STEPS:
            try:
                call_command(step)
            except Exception:  # noqa: BLE001 — one bad step must not skip the rest
                logger.exception("autopilot_step_failed", step=step)
                self.stderr.write(self.style.ERROR(f"{step} failed — see logs."))
