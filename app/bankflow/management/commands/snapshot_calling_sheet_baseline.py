"""
manage.py snapshot_calling_sheet_baseline -- the daily 06:00 IST baseline capture.

Copies each tracked CSP's current Calling Sheet value (latest DailyBalance row for
the date, DEMO/carry-forward excluded) into BaselineSnapshot. Insert-only: the
first capture for (csp, date) wins, so rerunning is a no-op and nothing observed
is overwritten. Each run is recorded in IngestLog (source=baseline_snapshot),
including failures.

Scheduling is NOT enabled by this repo; see docs/BANK-FLOW.md for the cron line.
`--not-before HH:MM` (IST) makes an early invocation a harmless no-op.
"""

from __future__ import annotations

import datetime as dt

import structlog
from csp.models import Csp, DailyBalance, IngestLog
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from bankflow.models import BaselineSnapshot

logger = structlog.get_logger("bankflow")
_NOT_OBSERVATIONS = [DailyBalance.Source.DEMO, DailyBalance.Source.CARRY_FORWARD]


class Command(BaseCommand):
    help = "Capture the 06:00 IST Calling Sheet baseline snapshot (idempotent, insert-only)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Snapshot date YYYY-MM-DD (default: today, IST).")
        parser.add_argument("--not-before", default="", help="HH:MM IST; earlier runs do nothing.")
        parser.add_argument(
            "--expected-at", default="",
            help="HH:MM IST it is meant to be taken; a capture >2 h later is noted as late.",
        )

    def handle(self, *args, **opts):
        today = timezone.localdate()
        day = dt.date.fromisoformat(opts["date"]) if opts["date"] else today
        if opts["not_before"] and day == today:
            hh, mm = map(int, opts["not_before"].split(":"))
            if timezone.localtime().time() < dt.time(hh, mm):
                self.stdout.write(f"before {opts['not_before']} IST; nothing done")
                return
        log = IngestLog.objects.create(
            source="baseline_snapshot", status=IngestLog.Status.PROCESSING
        )
        try:
            with transaction.atomic():
                tracked = set(Csp.objects.tracked().values_list("csp_code", flat=True))
                rows = list(
                    DailyBalance.objects.filter(balance_date=day, csp_id__in=tracked)
                    .exclude(source__in=_NOT_OBSERVATIONS)
                    .values_list("csp_id", "daily_avg_balance", "account_count")
                )
                existing = set(
                    BaselineSnapshot.objects.filter(snapshot_date=day).values_list(
                        "csp_id", flat=True
                    )
                )
                new = [
                    BaselineSnapshot(csp_id=c, snapshot_date=day, avg_balance=v, account_count=n)
                    for c, v, n in rows
                    if c not in existing
                ]
                BaselineSnapshot.objects.bulk_create(new, ignore_conflicts=True)
                seen = {c for c, _, _ in rows}
            log.rows_read = len(tracked)
            log.rows_valid = len(seen)
            log.rows_upserted = len(new)
            log.rows_duplicate = len(seen & existing)
            log.rows_rejected = len(tracked - seen)  # tracked CSPs with no reading that day
            log.status = IngestLog.Status.SUCCESS if seen else IngestLog.Status.PARTIAL
            if not seen:
                log.error_summary = f"no Calling Sheet readings for {day}"
            elif opts["expected_at"] and day == today:
                hh, mm = map(int, opts["expected_at"].split(":"))
                late = timezone.localtime() - timezone.localtime().replace(
                    hour=hh, minute=mm, second=0, microsecond=0)
                if late > dt.timedelta(hours=2):
                    hours = late.total_seconds() / 3600
                    log.error_summary = (
                        f"captured {hours:.1f} h after the {opts['expected_at']} IST target "
                        "(worker was down at that time)")
        except Exception as exc:
            log.status = IngestLog.Status.FAILED
            log.error_summary = str(exc)[:1000]
            log.finished_at = timezone.now()
            log.save()
            logger.exception("baseline_snapshot_failed", stage="SNAPSHOT", date=str(day))
            raise
        log.finished_at = timezone.now()
        log.save()
        self.stdout.write(
            f"{day}: inserted={log.rows_upserted} already_present={log.rows_duplicate} "
            f"no_reading={log.rows_rejected} status={log.status}"
        )
