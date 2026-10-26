"""
manage.py freeze_predictions -- save the shadow estimates as immutable predictions.

By default every FINAL estimate of the last 14 days that is not saved yet; the estimate of a day
whose bank statement is not complete is skipped (`--include-provisional` saves it too, labelled
provisional). Safe to repeat. Writes only learning.LivePrediction. Nothing schedules this command.
"""

import datetime as dt

from django.core.management.base import BaseCommand

from learning.predictions import SWEEP_DAYS, freeze_predictions


class Command(BaseCommand):
    help = "Freeze daily Avg Balance estimates as immutable predictions (insert-only)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Only this estimate date (YYYY-MM-DD).")
        parser.add_argument("--since-days", type=int, default=SWEEP_DAYS)
        parser.add_argument("--include-provisional", action="store_true")

    def handle(self, *args, **opts):
        day = dt.date.fromisoformat(opts["date"]) if opts["date"] else None
        r = freeze_predictions(day, since_days=opts["since_days"],
                               include_provisional=opts["include_provisional"])
        if r.get("error"):
            self.stderr.write(r["error"])
        self.stdout.write(
            f"created={r['created']} already_frozen={r['already_frozen']} "
            f"skipped_unavailable={r['skipped_unavailable']} "
            f"skipped_provisional={r['skipped_provisional']}")
