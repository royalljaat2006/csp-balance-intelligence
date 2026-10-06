"""
manage.py sync_daily_snapshots — populates DailyCspSnapshot, the daily-slab
history behind the CSP Daily Comparison feature (2026-09-18 platform
extension).

Reads DailyBalance (already the canonical daily-balance history — no new
input data) and writes one DailyCspSnapshot row per CSP for the given
business day, via csp/comparison.py's build_daily_snapshots_bulk() (a
handful of batched queries, not one per CSP — see that function's
docstring). Safe to re-run for the same date: every write is an upsert
keyed on (csp, business_date).

Run order (see docs/DEPLOYMENT.md "Scheduled jobs"): ingest -> `dbt run` ->
sync_monthly_summary -> this command, once per day after DailyBalance for
the day is in.
"""

from __future__ import annotations

import datetime as dt

import structlog
from django.core.management.base import BaseCommand, CommandError

from csp.comparison import build_daily_snapshots_bulk

logger = structlog.get_logger("ingestion")


class Command(BaseCommand):
    help = "Build DailyCspSnapshot rows (MTD-so-far slab) for one business day, all CSPs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            dest="business_date",
            help="Business date to snapshot, YYYY-MM-DD (default: today).",
        )

    def handle(self, *args, **options):
        raw_date = options.get("business_date")
        if raw_date:
            try:
                business_date = dt.date.fromisoformat(raw_date)
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD, got {raw_date!r}") from exc
        else:
            business_date = dt.date.today()

        logger.info("started", stage="DAILY_SNAPSHOT", business_date=str(business_date))

        snapshots = build_daily_snapshots_bulk(business_date)

        logger.info(
            "finished",
            stage="DAILY_SNAPSHOT",
            business_date=str(business_date),
            rows_synced=len(snapshots),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {len(snapshots)} DailyCspSnapshot row(s) for {business_date}."
            )
        )
