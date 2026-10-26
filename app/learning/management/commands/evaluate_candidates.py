"""
manage.py evaluate_candidates -- compare the registered estimation methods on SBI refreshes.

Time-ordered, parameter-free, held out: each method predicts a CSP's next SBI value from the
previous reading, the account counts and the statement movement, and is scored against what SBI
reported. Prints each method's verdict (RECOMMENDED / NOT_RECOMMENDED / INSUFFICIENT_EVIDENCE /
BLOCKED). Stores one insert-only evaluation per method unless --dry-run. It changes no active
method: promotion is `promote_model`, which needs a superuser.
"""

import datetime as dt

from django.core.management.base import BaseCommand

from learning.candidates import evaluate_candidates


class Command(BaseCommand):
    help = "Evaluate candidate estimation methods against carry-forward (never promotes)."

    def add_arguments(self, parser):
        parser.add_argument("--holdout-from", help="Score only refreshes on or after YYYY-MM-DD.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute and print, store nothing.")

    def handle(self, *args, **opts):
        holdout = dt.date.fromisoformat(opts["holdout_from"]) if opts["holdout_from"] else None
        for ev in evaluate_candidates(holdout_from=holdout, persist=not opts["dry_run"]):
            self.stdout.write(f"{ev.model.name}: {ev.verdict} -- {ev.detail}")
        if opts["dry_run"]:
            self.stdout.write("Dry run: nothing was stored.")
