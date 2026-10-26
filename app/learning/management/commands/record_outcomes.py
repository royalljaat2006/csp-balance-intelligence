"""
manage.py record_outcomes -- score saved predictions against the SBI values that followed.

A prediction is scored only when its CSP's NEXT Avg Balance refresh has been seen, and only if the
prediction was made strictly before the day that value was first seen. Predictions that already
have an outcome are never touched. Writes only learning.PredictionOutcome. Nothing schedules this.
"""

from django.core.management.base import BaseCommand

from learning.outcomes import record_outcomes


class Command(BaseCommand):
    help = "Match predictions to later SBI-observed values and score them (insert-only)."

    def handle(self, *args, **opts):
        r = record_outcomes()
        self.stdout.write(
            f"pending={r['pending']} scored={r['scored']} "
            f"waiting_for_next_refresh={r['waiting_for_next_refresh']} "
            f"unmatched_baseline={r['unmatched_baseline']} "
            f"not_before_outcome={r['not_before_outcome']} by_quality={r['by_quality']}")
