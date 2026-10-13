"""
manage.py build_bank_flows -- re-run KO-code resolution over every stored
statement transaction (after new CSPs appear, or after the variant rule is
confirmed) and rebuild the CSP-day flow aggregates. Original ko_raw values are
never altered; aggregates are deterministic and reproducible.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from bankflow import flows
from bankflow.ingest import reresolve_all


class Command(BaseCommand):
    help = "Re-resolve KO codes and rebuild bankflow CSP-day aggregates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aggregates-only",
            action="store_true",
            help="Rebuild aggregates without re-running code resolution.",
        )

    def handle(self, *args, **options):
        if options["aggregates_only"]:
            out = flows.rebuild_aggregates()
        else:
            out = reresolve_all()
        self.stdout.write(self.style.SUCCESS(str(out)))
