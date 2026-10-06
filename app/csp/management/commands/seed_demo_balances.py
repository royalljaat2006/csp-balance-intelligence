"""
manage.py seed_demo_balances — dev/demo only.

Generates synthetic DailyBalance rows for CSPs that already exist (e.g. from
`ingest_transactions`), spread deliberately across every Rule #19 slab, so
the dbt calculation layer (daily_activity, monthly_summary) can be built and
tested before real CALLING SHEET access is available (PRD §16 Q1/Q8).

Never run against a real deployment with live CALLING SHEET data — it
overwrites daily_balance rows with `source="demo"` fabricated numbers. Refuses
to run unless config.settings.development is active, as a guardrail.
"""

from __future__ import annotations

import calendar
import datetime as dt
import decimal
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from csp.models import Csp, DailyBalance

# Deliberately spans every slab band (PRD §5.1) so list/overview/incentive
# endpoints have something interesting to show.
_BAND_TARGETS = [2000, 3000, 5000, 8000, 12000]


class Command(BaseCommand):
    help = "Dev/demo only: seed synthetic daily balances across every Rule #19 slab."

    def add_arguments(self, parser):
        parser.add_argument("--month", default=None, help="YYYY-MM, defaults to current month")
        parser.add_argument("--limit", type=int, default=50, help="Number of CSPs to seed")

    def handle(self, *args, **options):
        if settings.DEBUG is not True:
            raise CommandError(
                "seed_demo_balances only runs under config.settings.development (DEBUG=True) — "
                "refusing to fabricate data anywhere that might be a real deployment."
            )

        month = options["month"] or dt.date.today().strftime("%Y-%m")
        year, mon = (int(part) for part in month.split("-"))
        days_in_month = calendar.monthrange(year, mon)[1]

        csps = list(Csp.objects.all()[: options["limit"]])
        if not csps:
            raise CommandError("No CSPs exist yet — run ingest_transactions first.")

        created = 0
        for i, csp in enumerate(csps):
            target = _BAND_TARGETS[i % len(_BAND_TARGETS)]
            for day in range(1, days_in_month + 1):
                balance = decimal.Decimal(target) + decimal.Decimal(random.uniform(-150, 150))
                _, made = DailyBalance.objects.update_or_create(
                    csp=csp,
                    balance_date=dt.date(year, mon, day),
                    defaults={"daily_avg_balance": round(balance, 2), "source": "demo"},
                )
                created += 1 if made else 0

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(csps)} CSPs x {days_in_month} days for {month} "
                f"(source='demo', {created} new rows)."
            )
        )
