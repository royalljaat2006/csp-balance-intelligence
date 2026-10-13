"""
manage.py evaluate_account_count_hypothesis -- test "an account-count rise moves
Avg Balance proportionally (350 -> 360 = +2.857%)" against DailyBalance history
and record the verdict (HypothesisRun). Read-only on balance data.
"""

from __future__ import annotations

import datetime as dt
import json

from django.core.management.base import BaseCommand

from bankflow.account_hypothesis import run_hypothesis_test


class Command(BaseCommand):
    help = "Score the account-count proportionality hypothesis against observed refreshes."

    def add_arguments(self, parser):
        parser.add_argument("--until", help="Only use observations up to YYYY-MM-DD.")
        parser.add_argument("--no-persist", action="store_true", help="Print only; store nothing.")
        parser.add_argument("--json", action="store_true", help="Print the full metrics as JSON.")

    def handle(self, *args, **opts):
        until = dt.date.fromisoformat(opts["until"]) if opts["until"] else None
        metrics, status, detail, run = run_hypothesis_test(
            persist=not opts["no_persist"], until=until
        )
        self.stdout.write(
            f"status={status} pairs={metrics['n_pairs']} events={metrics['n_events']}"
        )
        self.stdout.write(detail)
        if opts["json"]:
            self.stdout.write(json.dumps(metrics, indent=2, default=str))
