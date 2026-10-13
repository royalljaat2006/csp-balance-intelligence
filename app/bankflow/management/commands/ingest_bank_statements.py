"""
manage.py ingest_bank_statements -- read SBI pool-account statements from the
watch folder (READ-ONLY), reconcile, quarantine failures, resolve KO codes,
and rebuild flow aggregates. Safe to re-run: identical files are recognised by
SHA-256. Source files are never moved, renamed or deleted.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from bankflow.ingest import run_import


class Command(BaseCommand):
    help = "Ingest bank statements from a folder (or one file) into the bankflow tables."

    def add_arguments(self, parser):
        parser.add_argument("--path", help="Folder or single file (default: BANKFLOW_WATCH_DIR).")
        parser.add_argument(
            "--min-age-seconds",
            type=int,
            default=5,
            help="Skip files modified more recently than this (still being written).",
        )
        parser.add_argument(
            "--recursive",
            action="store_true",
            help="Also read sub-folders (read-only; nothing is created or moved).",
        )
        parser.add_argument(
            "--pattern",
            default=None,
            help="Only files whose NAME matches this glob, e.g. 'statement_1556_*'.",
        )
        parser.add_argument(
            "--max-file-mb",
            type=int,
            default=None,
            help="Raise the per-file safety limit (default 50 MB; a full month is ~75 MB).",
        )
        parser.add_argument(
            "--retry-quarantined",
            action="store_true",
            help="Re-evaluate quarantined/failed files (replaces our audit record only).",
        )

    def handle(self, *args, **options):
        path = options["path"] or settings.BANKFLOW_WATCH_DIR
        run = run_import(
            path,
            min_age_seconds=options["min_age_seconds"],
            retry_quarantined=options["retry_quarantined"],
            max_file_mb=options["max_file_mb"],
            recursive=options["recursive"],
            pattern=options["pattern"],
        )
        if run.error_summary:
            self.stderr.write(run.error_summary)
        self.stdout.write(
            self.style.SUCCESS(
                f"seen={run.files_seen} imported={run.files_imported} "
                f"duplicate={run.files_duplicate} quarantined={run.files_quarantined} "
                f"failed={run.files_failed} skipped={run.files_skipped}"
            )
        )
