"""
manage.py evaluate_account_count_hypothesis -- score the movement estimator against
carry-forward on held-out SBI refreshes (DailyBalance history + bank statements) and record the
verdict (HypothesisRun). The old account-count rule is kept only as a stored diagnostic.
Read-only on balance data.
"""

from __future__ import annotations

import datetime as dt
import json

from django.core.management.base import BaseCommand

from bankflow.account_hypothesis import run_hypothesis_test


class Command(BaseCommand):
    help = "Score the movement estimator against carry-forward on observed SBI refreshes."

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
