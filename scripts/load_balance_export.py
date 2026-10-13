"""
Load a read-only CSV export of the DEPLOYED tracker's csp_csp / csp_dailybalance into a LOCAL
database, so the account-count and estimator checks can run on real history.

This never talks to the deployed system: you export two small CSVs there (see docs/BANK-FLOW.md,
"Getting Calling Sheet history"), copy them here, then run:

    python scripts/load_balance_export.py --csps csp.csv --balances dailybalance.csv

Safety: refuses any database that is not on localhost (override only with --allow-nonlocal-db),
upserts idempotently, skips DEMO / carry-forward rows (not SBI observations), and never reads or
needs names, mobiles or e-mails -- the export columns below contain none.

csp.csv           csp_code, status, account_count, zone, first_seen_date, last_seen_date
dailybalance.csv  csp_id, balance_date, daily_avg_balance, account_count, source
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import decimal
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP_SOURCES = {"demo", "carry_forward"}
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _int(v: str) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _date(v: str) -> dt.date | None:
    v = (v or "").strip()
    return dt.date.fromisoformat(v[:10]) if v else None


def local_db_only(settings_dict: dict) -> bool:
    return (settings_dict.get("HOST") or "") in LOCAL_HOSTS


def load(csps_path: Path, balances_path: Path) -> dict:
    from csp.models import Csp, DailyBalance
    from django.db import transaction

    stats = {"csps": 0, "balances": 0, "skipped_source": 0, "skipped_unknown_csp": 0,
             "skipped_bad_row": 0}
    with transaction.atomic():
        with open(csps_path, newline="", encoding="utf-8-sig") as fh:
            rows = []
            for r in csv.DictReader(fh):
                code = (r.get("csp_code") or "").strip()
                if not code:
                    continue
                rows.append(Csp(
                    csp_code=code, status=(r.get("status") or "").strip(),
                    account_count=_int(r.get("account_count", "")),
                    zone=(r.get("zone") or "").strip(),
                    first_seen_date=_date(r.get("first_seen_date", "")),
                    last_seen_date=_date(r.get("last_seen_date", "")),
                ))
            Csp.objects.bulk_create(
                rows, update_conflicts=True, unique_fields=["csp_code"],
                update_fields=["status", "account_count", "zone", "first_seen_date",
                               "last_seen_date"], batch_size=1000)
            stats["csps"] = len(rows)
        known = set(Csp.objects.values_list("csp_code", flat=True))
        batch: list[DailyBalance] = []
        with open(balances_path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                try:
                    code = (r["csp_id"] or "").strip()
                    day = _date(r["balance_date"])
                    value = decimal.Decimal((r["daily_avg_balance"] or "").strip())
                except (KeyError, ValueError, decimal.InvalidOperation):
                    stats["skipped_bad_row"] += 1
                    continue
                source = (r.get("source") or "").strip()
                if source in SKIP_SOURCES:
                    stats["skipped_source"] += 1
                elif code not in known or day is None:
                    stats["skipped_unknown_csp"] += 1
                else:
                    batch.append(DailyBalance(
                        csp_id=code, balance_date=day, daily_avg_balance=value,
                        account_count=_int(r.get("account_count", "")), source=source or "history"))
                if len(batch) >= 2000:
                    _flush(DailyBalance, batch, stats)
        _flush(DailyBalance, batch, stats)
    return stats


def _flush(model, batch: list, stats: dict) -> None:
    if batch:
        model.objects.bulk_create(
            batch, update_conflicts=True, unique_fields=["csp", "balance_date"],
            update_fields=["daily_avg_balance", "account_count", "source"], batch_size=1000)
        stats["balances"] += len(batch)
        batch.clear()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--csps", required=True, type=Path)
    ap.add_argument("--balances", required=True, type=Path)
    ap.add_argument("--allow-nonlocal-db", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO / "app"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    import django

    django.setup()
    from django.conf import settings

    if not local_db_only(settings.DATABASES["default"]) and not args.allow_nonlocal_db:
        sys.exit("Refusing: the configured database is not on localhost. This tool is for local "
                 "analysis only.")
    stats = load(args.csps, args.balances)
    from csp.models import DailyBalance
    from django.db.models import Count, Max, Min

    span = DailyBalance.objects.aggregate(lo=Min("balance_date"), hi=Max("balance_date"),
                                          d=Count("balance_date", distinct=True))
    print("loaded:", stats)
    print("DailyBalance now spans", span["lo"], "..", span["hi"], f"({span['d']} distinct dates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
