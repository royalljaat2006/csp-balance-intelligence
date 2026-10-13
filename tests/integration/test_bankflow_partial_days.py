"""Complete / partial / unverified day classification: a day-to-date snapshot is never
reported or used as a daily total, and its coverage window is stated explicitly."""

import datetime as dt
import decimal
import os
from zoneinfo import ZoneInfo

import pytest
from bankflow import flows, services
from bankflow.ingest import run_import
from csp.models import Csp

from tests.bankflow_fixtures import desc_withdrawal, make_statement

D = decimal.Decimal
IST = ZoneInfo("Asia/Kolkata")
pytestmark = pytest.mark.django_db


@pytest.fixture
def csps(db):
    Csp.objects.create(csp_code="1A852455", name="c", account_count=250)


def _stmt(tmp_path, name, day: dt.date, opening, credits, exported: dt.datetime):
    """One-day statement for `day`, whose file timestamp (export time) is `exported`."""
    rows = [
        (desc_withdrawal(f"62850709{day:%d%m}{i:08d}", "1A852455"), "", f"{c:.2f}")
        for i, c in enumerate(credits)
    ]
    text = make_statement(
        rows,
        opening=str(opening),
        start=f"{day.day} {day:%b} {day.year}",
        txn_date=f"{day:%d/%m/%Y}",
    )
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    ts = exported.timestamp()
    os.utime(p, (ts, ts))
    return p, D(opening) + sum(D(str(c)) for c in credits)


def test_same_day_export_is_a_partial_day_with_explicit_coverage(tmp_path, csps):
    day = dt.date(2026, 10, 12)
    _stmt(
        tmp_path, "a.xls", day, "1000.00", [100, 200], dt.datetime(2026, 10, 12, 14, 40, tzinfo=IST)
    )
    run_import(tmp_path, min_age_seconds=0)
    cov = services.day_coverage()[day]
    assert cov["class"] == "PARTIAL_DAY"
    assert cov["coverage_start"] == dt.datetime(2026, 10, 12, 0, 0, tzinfo=IST)
    assert cov["coverage_end_at_most"] == dt.datetime(2026, 10, 12, 14, 40, tzinfo=IST)
    assert cov["coverage_hours"] == 14.7
    assert day not in flows.proven_complete_dates()


def test_next_day_export_without_a_chain_is_unverified_not_complete(tmp_path, csps):
    day = dt.date(2026, 9, 17)
    _stmt(tmp_path, "a.xls", day, "1000.00", [100], dt.datetime(2026, 9, 18, 11, 55, tzinfo=IST))
    run_import(tmp_path, min_age_seconds=0)
    cov = services.day_coverage()[day]
    assert cov["class"] == "UNVERIFIED"
    assert "no continuity proof" in cov["reason"]


def test_chain_to_the_next_statement_upgrades_the_day_to_complete(tmp_path, csps):
    d1, d2 = dt.date(2026, 9, 22), dt.date(2026, 9, 23)
    _, closing = _stmt(
        tmp_path, "d1.xls", d1, "1000.00", [100, 50], dt.datetime(2026, 9, 23, 17, 31, tzinfo=IST)
    )
    _stmt(tmp_path, "d2.xls", d2, str(closing), [10], dt.datetime(2026, 9, 24, 11, 2, tzinfo=IST))
    run_import(tmp_path, min_age_seconds=0)
    cov = services.day_coverage()
    assert cov[d1]["class"] == "COMPLETE_DAY"
    assert cov[d2]["class"] == "UNVERIFIED"  # exported the next day, nothing after it yet


def test_a_same_day_cut_that_does_not_chain_stays_partial(tmp_path, csps):
    d1, d2 = dt.date(2026, 10, 5), dt.date(2026, 10, 6)
    _stmt(tmp_path, "d1.xls", d1, "1000.00", [100], dt.datetime(2026, 10, 5, 18, 31, tzinfo=IST))
    # next day opens at a balance that differs from the 18:31 closing (postings after the cut)
    _stmt(tmp_path, "d2.xls", d2, "4000.00", [10], dt.datetime(2026, 10, 6, 12, 33, tzinfo=IST))
    run_import(tmp_path, min_age_seconds=0)
    cov = services.day_coverage()
    assert cov[d1]["class"] == "PARTIAL_DAY" and cov[d2]["class"] == "PARTIAL_DAY"
    assert flows.proven_complete_dates() == set()


def test_partial_snapshot_report_states_coverage_and_never_a_daily_total(tmp_path, csps):
    day = dt.date(2026, 10, 12)
    _stmt(
        tmp_path, "a.xls", day, "1000.00", [100, 200], dt.datetime(2026, 10, 12, 14, 40, tzinfo=IST)
    )
    run_import(tmp_path, min_age_seconds=0)
    [snap] = services.partial_snapshot_report()
    assert snap["rows"] == 2 and snap["pool_credit"] == D("300.00")
    assert snap["coverage_hours"] == 14.7 and snap["cut_hour"] == 14
    rep = services.coverage_report()
    assert (rep["complete_days"], rep["partial_days"], rep["unverified_days"]) == (0, 1, 0)


def test_partial_days_never_enter_evaluation_windows(tmp_path, csps):
    day = dt.date(2026, 10, 12)
    _stmt(tmp_path, "a.xls", day, "1000.00", [100], dt.datetime(2026, 10, 12, 14, 40, tzinfo=IST))
    run_import(tmp_path, min_age_seconds=0)
    got, coverage = flows.window_flows(["1A852455"], dt.date(2026, 10, 10), dt.date(2026, 10, 12))
    assert got == {} and coverage == 0.0  # the snapshot contributes nothing to a window
    got_all, _ = flows.window_flows(
        ["1A852455"], dt.date(2026, 10, 10), dt.date(2026, 10, 12), complete_only=False
    )
    assert got_all["1A852455"] == D(
        "-100.00"
    )  # visible only when explicitly asked, never by default


def test_multi_day_statement_interior_days_are_complete_and_its_last_day_follows_the_rules(
    tmp_path, csps
):
    rows = [
        (desc_withdrawal("62850709168200000001", "1A852455"), "", "100.00", "01/08/2026"),
        (desc_withdrawal("62850709168200000002", "1A852455"), "", "70.00", "02/08/2026"),
    ]
    text = make_statement(rows, opening="1000.00", start="01 Aug 2026", end="02 Aug 2026")
    p = tmp_path / "m.xls"
    p.write_text(text, encoding="utf-8")
    ts = dt.datetime(2026, 9, 16, 15, 18, tzinfo=IST).timestamp()
    os.utime(p, (ts, ts))
    run_import(tmp_path, min_age_seconds=0)
    cov = services.day_coverage()
    assert cov[dt.date(2026, 8, 1)]["class"] == "COMPLETE_DAY"
    assert cov[dt.date(2026, 8, 2)]["class"] == "UNVERIFIED"  # exported later, no next statement
