"""
manage.py flag_invalid_csp_codes -- mark CSP rows whose code is not "1A85" + 4 digits.

A real CSP/KO code is always 1A85XXXX (8 characters). Rows with any other code (for example a stray
"f" that once came from the Calling Sheet) get status="invalid_code", which keeps them out of every
CSP listing, count and estimate (Csp.objects.tracked()). Nothing is deleted; safe to re-run.
Use --dry-run to only list them.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from csp.models import CSP_CODE_RE, INVALID_CODE_STATUS, Csp


class Command(BaseCommand):
    help = "Flag CSP rows whose code is not 1A85 + 4 digits (idempotent, nothing deleted)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List only; change nothing.")

    def handle(self, *args, **opts):
        codes = Csp.objects.values_list("csp_code", flat=True)
        bad = [c for c in codes if not CSP_CODE_RE.match(c)]
        already = Csp.objects.filter(csp_code__in=bad, status=INVALID_CODE_STATUS).count()
        shown = sorted(bad)[:20]
        self.stdout.write(f"{len(bad)} invalid code(s): {shown} ({already} already flagged)")
        if bad and not opts["dry_run"]:
            n = Csp.objects.filter(csp_code__in=bad).exclude(status=INVALID_CODE_STATUS).update(
                status=INVALID_CODE_STATUS
            )
            self.stdout.write(f"flagged {n}")
