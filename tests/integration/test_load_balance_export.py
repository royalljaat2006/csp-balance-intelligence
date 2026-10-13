"""scripts/load_balance_export.py: local-only, idempotent, drops non-observations, no PII."""

import datetime as dt
import decimal
import importlib.util
import sys
from pathlib import Path

import pytest
from csp.models import Csp, DailyBalance

SPEC = importlib.util.spec_from_file_location(
    "load_balance_export",
    Path(__file__).resolve().parents[2] / "scripts" / "load_balance_export.py",
)
loader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = loader
SPEC.loader.exec_module(loader)

pytestmark = pytest.mark.django_db


def _files(tmp_path):
    csps = tmp_path / "csp.csv"
    csps.write_text(
        "csp_code,status,account_count,zone,first_seen_date,last_seen_date\n"
        "1A850001,,350,North,2026-07-14,2026-10-13\n"
        "1A850002,historical_only,,,,\n",
        encoding="utf-8")
    bal = tmp_path / "dailybalance.csv"
    bal.write_text(
        "csp_id,balance_date,daily_avg_balance,account_count,source\n"
        "1A850001,2026-07-14,4000.50,300,history\n"
        "1A850001,2026-09-22,4100.00,350,calling_sheet\n"
        "1A850001,2026-09-23,4100.00,351,carry_forward\n"
        "1A850001,2026-09-24,9999,351,demo\n"
        "NOPE,2026-09-22,10,1,calling_sheet\n"
        "1A850001,not-a-date,10,1,calling_sheet\n",
        encoding="utf-8")
    return csps, bal


def test_load_keeps_only_sbi_observations_for_known_csps_and_reports_skips(tmp_path):
    csps, bal = _files(tmp_path)
    stats = loader.load(csps, bal)
    assert stats == {"csps": 2, "balances": 2, "skipped_source": 2, "skipped_unknown_csp": 1,
                     "skipped_bad_row": 1}
    rows = {r.balance_date: r for r in DailyBalance.objects.all()}
    assert set(rows) == {dt.date(2026, 7, 14), dt.date(2026, 9, 22)}
    assert rows[dt.date(2026, 7, 14)].daily_avg_balance == decimal.Decimal("4000.50")
    assert rows[dt.date(2026, 9, 22)].account_count == 350
    assert Csp.objects.get(csp_code="1A850002").status == "historical_only"
    assert Csp.objects.get(csp_code="1A850001").name == ""  # no names are imported


def test_load_is_idempotent_and_updates_in_place(tmp_path):
    csps, bal = _files(tmp_path)
    loader.load(csps, bal)
    loader.load(csps, bal)
    assert DailyBalance.objects.count() == 2 and Csp.objects.count() == 2
    bal.write_text(bal.read_text().replace("4100.00,350", "4200.00,352"), encoding="utf-8")
    loader.load(csps, bal)
    assert DailyBalance.objects.get(balance_date=dt.date(2026, 9, 22)).daily_avg_balance == 4200


def test_remote_databases_are_refused():
    assert loader.local_db_only({"HOST": "127.0.0.1"})
    assert loader.local_db_only({"HOST": "localhost"})
    assert loader.local_db_only({"HOST": ""})
    assert not loader.local_db_only({"HOST": "db.internal.example"})
    assert not loader.local_db_only({"HOST": "122.176.147.78"})
