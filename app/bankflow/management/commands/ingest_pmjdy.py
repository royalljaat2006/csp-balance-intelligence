"""
manage.py ingest_pmjdy -- capture this month's NEW PMJDY accounts per CSP (month-to-date,
for-the-day, target) from the Calling Sheet's monthly blocks.

Read-only against Google Sheets; writes only PmjdyMonthly (insert-only) and one IngestLog row
(source=pmjdy) per real attempt. Safe to rerun. Rate-limited: within PMJDY_MIN_INTERVAL_SECONDS of
the last successful read it does nothing (use --force to override). If this month's
block is missing, or the read fails, nothing is written (last month's numbers are never
reused). NOT scheduled by this repo; see docs/BANK-FLOW.md.
"""

from __future__ import annotations

import datetime as dt

import gspread
import structlog
from csp.models import IngestLog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bankflow.pmjdy_monthly import PmjdyValidationError, capture_monthly, parse_monthly_pmjdy

logger = structlog.get_logger("bankflow")
_TIMEOUT = 30


class Command(BaseCommand):
    help = "Capture this month's new PMJDY accounts per CSP (insert-only history)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Ignore the minimum interval.")

    def handle(self, *args, **opts):
        missing = [n for n in ("GOOGLE_SERVICE_ACCOUNT_FILE", "CALLING_SHEET_ID")
                   if not getattr(settings, n)]
        if missing:
            raise CommandError(f"Cannot run ingest_pmjdy: {', '.join(missing)} not configured.")

        last = (
            IngestLog.objects.filter(source="pmjdy", status=IngestLog.Status.SUCCESS)
            .order_by("-started_at").values_list("started_at", flat=True).first()
        )
        interval = settings.PMJDY_MIN_INTERVAL_SECONDS
        if last and not opts["force"] and (timezone.now() - last) < dt.timedelta(seconds=interval):
            self.stdout.write(
                f"last successful read {last:%H:%M}; within {interval}s, nothing done"
            )
            return

        log = IngestLog.objects.create(source="pmjdy", status=IngestLog.Status.FAILED)
        try:
            gc = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
            gc.set_timeout(_TIMEOUT)
            values = (
                gc.open_by_key(settings.CALLING_SHEET_ID)
                .worksheet(settings.CALLING_SHEET_WORKSHEET_NAME)
                .get_all_values()
            )
        except Exception as exc:  # noqa: BLE001 -- any Sheets failure is a rejected run, no writes
            self._fail(log, f"Could not read the Calling Sheet: {type(exc).__name__}: {exc}")
            return
        try:
            parsed = parse_monthly_pmjdy(values, timezone.localdate(), column=settings.PMJDY_COLUMN)
        except PmjdyValidationError as exc:
            self._fail(log, str(exc))
            return
        if not parsed.rows:
            self._fail(log, "The PMJDY block parsed to zero usable rows; nothing captured.")
            return

        stats = capture_monthly(parsed)
        log.status = IngestLog.Status.SUCCESS
        log.file_name = f"{settings.CALLING_SHEET_WORKSHEET_NAME}: {parsed.labels['mtd']}"[:255]
        log.rows_read = parsed.rows_read
        log.rows_valid = len(parsed.rows)
        log.rows_rejected = parsed.skipped_blank_code
        log.rows_upserted = stats["inserted"]
        log.rows_duplicate = stats["unchanged_not_inserted"]
        log.rows_unmapped_csp = stats["n_unknown_codes"]
        notes = []
        if parsed.conflicting_codes:
            n_conf = len(parsed.conflicting_codes)
            notes.append(f"{n_conf} CSP(s) listed twice with different numbers")
        if stats["mtd_decreased"]:
            notes.append(f"{stats['mtd_decreased']} month-to-date figure(s) DECREASED")
        log.error_summary = "; ".join(notes)
        log.finished_at = timezone.now()
        log.save()
        logger.info("pmjdy_captured", stage="PMJDY", month=parsed.month,
                    **{k: v for k, v in stats.items() if k != "unknown_codes"})
        self.stdout.write(
            f"PMJDY {parsed.month} ({parsed.labels['mtd']}): inserted={stats['inserted']} "
            f"changed={stats['changed']} unchanged={stats['unchanged_not_inserted']} "
            f"unknown_codes={stats['n_unknown_codes']}"
            + (f" | {log.error_summary}" if notes else "")
        )

    def _fail(self, log: IngestLog, message: str) -> None:
        log.error_summary = message[:1000]
        log.finished_at = timezone.now()
        log.save()
        self.stderr.write(message)
