"""
manage.py bank_flow_coverage -- is there enough history to evaluate flow-based
prediction? Prints measured statement/Calling Sheet coverage, missing intervals,
overlap, independent refresh events with usable flow data, and attribution
coverage. Read-only.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from bankflow import services


class Command(BaseCommand):
    help = "Report statement + Calling Sheet history coverage and evaluation readiness (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--min-events", type=int, default=8)

    def handle(self, *args, **opts):
        rep = services.coverage_report(opts["min_events"])
        partial = rep.pop("partial_snapshots")
        for key, value in rep.items():
            self.stdout.write(f"{key}: {value}")
        if partial:
            self.stdout.write(
                "partial (day-to-date) snapshots -- analysed separately, never as daily totals:"
            )
            for p in partial:
                self.stdout.write(
                    f"  {p['date']}  rows={p['rows']:>6}  exported {p['exported_at']:%H:%M}  "
                    f"covers 00:00 -> at most {p['coverage_end_at_most']:%H:%M} "
                    f"({p['coverage_hours']} h)  pool credit={p['pool_credit']}  "
                    f"debit={p['pool_debit']}"
                )
        verdict = "READY to evaluate" if rep["ready_for_evaluation"] else "NOT ready"
        self.stdout.write(
            self.style.SUCCESS(
                f"=> {verdict}; {rep['events_still_needed']} more usable event(s) needed"
            )
        )
