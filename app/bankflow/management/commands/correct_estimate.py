"""
manage.py correct_estimate -- reconcile the SHADOW estimates after statement data for an
already-final day changed.

  --list-drift                       read only: final days whose stored movement differs from the
                                     statement data now (nothing is changed)
  --csp CODE --from-date D --reason "..." --actor USER
                                     recompute that CSP's current cycle from day D. The actor
                                     must be an active superuser; the reason is stored with
                                     the old values.
  --dry-run                          show what would change and write nothing

Writes only bankflow_dailyestimate and bankflow_estimatecorrection. Never touches DailyBalance,
MonthlySummary or Rule #19. Re-running with unchanged data writes nothing.
"""

from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError

from bankflow import corrections, estimates, movement
from bankflow.models import DailyEstimate


class Command(BaseCommand):
    help = "List drifted final estimates, or apply an authorised, audited correction."

    def add_arguments(self, parser):
        parser.add_argument("--list-drift", action="store_true",
                            help="Read only: list final days that differ from the statements now.")
        parser.add_argument("--csp", help="CSP code to correct.")
        parser.add_argument("--from-date", help="First day to recompute (YYYY-MM-DD).")
        parser.add_argument("--reason", help="Why (at least 10 characters). Stored permanently.")
        parser.add_argument("--actor", help="Username of the superuser authorising this.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show whether anything would change; write nothing.")

    def handle(self, *args, **opts):
        if opts["list_drift"]:
            items = corrections.estimate_drift(opts["csp"])
            if not items:
                self.stdout.write("No final estimate differs from the statement data.")
            for i in items:
                self.stdout.write(
                    f"{i['csp_code']} {i['date']}: stored +{i['stored_deposits']} "
                    f"-{i['stored_withdrawals']}, now +{i['current_deposits']} "
                    f"-{i['current_withdrawals']} (net {i['net_difference']:+})")
            for code, g in corrections.group_drift(items).items():
                self.stdout.write(f"  {code}: correct from {g['first']} "
                                  f"({len(g['days'])} day(s))")
            return
        missing = [f for f in ("csp", "from_date", "reason", "actor") if not opts.get(f)]
        if missing:
            raise CommandError("Required: " + ", ".join("--" + m.replace("_", "-")
                                                        for m in missing))
        try:
            from_date = dt.date.fromisoformat(opts["from_date"])
        except ValueError as exc:
            raise CommandError("--from-date must be YYYY-MM-DD") from exc
        actor = get_user_model().objects.filter(username=opts["actor"]).first()
        if not corrections.is_authorised(actor):
            raise CommandError("The actor must be an existing, active superuser.")
        if opts["dry_run"]:
            latest = DailyEstimate.objects.filter(
                csp_id=opts["csp"], method=movement.METHOD).order_by("-estimate_date").first()
            if latest is None:
                raise CommandError("This CSP has no movement estimate.")
            built, _ = estimates.build_estimates(
                latest.estimate_date, only_csp=opts["csp"], restart_from=from_date)
            old = {e.estimate_date: e for e in DailyEstimate.objects.filter(
                csp_id=opts["csp"], method=movement.METHOD, estimate_date__gte=from_date)}
            diff = [r["estimate_date"] for r in built.values()
                    if r["csp_id"] == opts["csp"] and r["estimate_date"] >= from_date
                    and (r["estimate_date"] not in old
                         or not corrections._same(old[r["estimate_date"]], r))]
            self.stdout.write(f"Dry run: {len(diff)} day(s) would change {sorted(diff)[:5]}")
            return
        try:
            rec = corrections.apply_correction(
                opts["csp"], from_date, reason=opts["reason"], actor=actor)
        except PermissionDenied as exc:
            raise CommandError(str(exc)) from exc
        except corrections.CorrectionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Correction #{rec.pk}: {rec.outcome}, {len(rec.affected_dates)} "
                          f"day(s) rewritten for {rec.csp_id}.")
