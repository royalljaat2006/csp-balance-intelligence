"""
manage.py warm_dashboard_cache -- pre-compute the dashboard's shared figures for the current data
generation, so the first visitor after an ingest does not wait for them. Idempotent and cheap when
nothing has changed (it exits at once). Run by the bank-flow worker on a short interval.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from dashboard.warm import warm_dashboard_cache


class Command(BaseCommand):
    help = "Pre-compute the cached dashboard figures for the current data generation."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Recompute even if this generation was already warmed.")

    def handle(self, *args, **opts):
        r = warm_dashboard_cache(force=opts["force"])
        if not r["warmed"]:
            self.stdout.write("cache already warm")
            return
        slow = ", ".join(f"{k} {v}" for k, v in r["steps"].items())
        self.stdout.write(f"warmed in {r['seconds']}s: {slow}")
