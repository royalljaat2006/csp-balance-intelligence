"""
manage.py ingest_transactions — PRD §8 FR2.

Finds .xlsx files in the watched incoming/ folder and hands each one to
ingestion/file_ingest.py's ingest_transaction_file() — the shared
validate/parse/upsert/file orchestrator also used by manage.py
poll_telegram, so folder-watch and Telegram-delivered files run through
exactly the same pipeline.
"""

from __future__ import annotations

from pathlib import Path

from csp.models import IngestLog
from django.conf import settings
from django.core.management.base import BaseCommand

from ingestion.file_ingest import ingest_transaction_file
from ingestion.file_safety import IngestionPaths


class Command(BaseCommand):
    help = "Validate and file transaction .xlsx files from the watched incoming/ folder."

    def handle(self, *args, **options):
        paths = IngestionPaths(root=Path(settings.TRANSACTION_DATA_DIR))
        paths.ensure_exist()

        candidates = sorted(paths.incoming.glob("*.xlsx"))
        if not candidates:
            self.stdout.write("No files in incoming/.")
            return

        for file_path in candidates:
            log = ingest_transaction_file(paths, file_path, source="transactions")
            if log.status == IngestLog.Status.FAILED:
                self.stderr.write(log.error_summary)
            else:
                self.stdout.write(
                    f"{file_path.name}: {log.rows_read} in-scope rows read, "
                    f"{log.rows_upserted} upserted, {log.rows_rejected} rejected."
                )
