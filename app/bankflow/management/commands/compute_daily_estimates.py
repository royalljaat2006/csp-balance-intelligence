"""
manage.py compute_daily_estimates -- build today's (IST) DailyEstimate rows.
Writes only bankflow_dailyestimate; never touches DailyBalance. Safe to rerun:
rows are upserted on (csp, estimate_date).
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand

from bankflow.estimates import compute_daily_estimates


class Command(BaseCommand):
    help = "Compute the daily Avg Balance estimates (separate from SBI-provided balances)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Estimate date YYYY-MM-DD (default: today, IST).")

    def handle(self, *args, **opts):
        d = dt.date.fromisoformat(opts["date"]) if opts["date"] else None
        r = compute_daily_estimates(d)
        self.stdout.write(
            f"{r['estimate_date']}: {r['rows']} rows {r['by_status']} "
            f"(account-count hypothesis: {r['hypothesis_status']})"
        )
