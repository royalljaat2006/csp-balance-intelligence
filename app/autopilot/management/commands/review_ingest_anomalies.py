"""manage.py review_ingest_anomalies — Nemotron review of recent ingestion
runs for signs of a data-entry or pipeline error (see autopilot/services.py).
Purely observational: never blocks or modifies the ingestion it reviews.

Run standalone, or right after ingest_calling_sheet/ingest_transactions —
see run_autopilot.py, which is what the worker loop actually calls.
"""

from __future__ import annotations

import structlog
from csp.models import IngestLog
from django.core.management.base import BaseCommand

from autopilot.services import flag_ingestion_anomalies

logger = structlog.get_logger("autopilot")


class Command(BaseCommand):
    help = "Review recent ingestion runs that haven't been reviewed yet, and flag anomalies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ingest-log-id", type=int, default=None, help="Review one specific run by id."
        )
        parser.add_argument(
            "--limit", type=int, default=3, help="How many recent unreviewed runs to check."
        )

    def handle(self, *args, **options):
        if options["ingest_log_id"] is not None:
            logs = IngestLog.objects.filter(pk=options["ingest_log_id"])
        else:
            logs = IngestLog.objects.filter(autopilot_review__isnull=True).order_by(
                "-started_at"
            )[: options["limit"]]

        total_flags = 0
        for log in logs:
            flags = flag_ingestion_anomalies(log)
            total_flags += len(flags)
            if flags:
                self.stdout.write(
                    self.style.WARNING(f"IngestLog #{log.id} ({log.source}): {len(flags)} flag(s).")
                )
        self.stdout.write(
            self.style.SUCCESS(f"Reviewed {len(logs)} run(s), {total_flags} flag(s).")
        )
