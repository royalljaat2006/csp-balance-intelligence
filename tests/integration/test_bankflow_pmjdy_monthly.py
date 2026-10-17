"""New PMJDY accounts per CSP: capture history, month isolation, the ingest command, and what the
dashboard says about them (plain wording, real numbers, honest when missing or stale)."""

import datetime as dt
from types import SimpleNamespace

import pytest
from bankflow import intelligence, pmjdy_monthly
from bankflow.models import PmjdyMonthly
from bankflow.pmjdy_monthly import MonthlyParse, capture_monthly, monthly_totals
from common.cache import bump_generation
from csp.models import Csp, IngestLog
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from tests.unit.test_pmjdy_parse import make_grid

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
MONTH = f"{TODAY:%Y-%m}"


def at(day, hour=6):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=timezone.get_current_timezone())


def parse(rows, month="2026-10"):
    return MonthlyParse(month=month, rows=rows)


@pytest.fixture
def csps():
    for c in ("A", "B"):
        Csp.objects.create(csp_code=c, name=c, account_count=300)


def test_capture_is_insert_only_idempotent_and_daily(csps):
    first = parse({"A": {"mtd": 5, "ftd": 1, "target": 7}, "B": {"mtd": 0, "ftd": 0, "target": 7}})
    assert capture_monthly(first, at(10, 6))["inserted"] == 2
    assert capture_monthly(first, at(10, 9))["inserted"] == 0  # same figures, same IST day
    grown = parse({"A": {"mtd": 9, "ftd": 4, "target": 7}, "B": {"mtd": 0, "ftd": 0, "target": 7}})
    stats = capture_monthly(grown, at(10, 15))
    assert stats["inserted"] == 1 and stats["changed"] == 1  # only A moved: new row, old kept
    assert PmjdyMonthly.objects.filter(csp_id="A").count() == 2
    assert capture_monthly(grown, at(11, 6))["inserted"] == 2  # a new day gets its snapshot
    latest = pmjdy_monthly.latest_monthly("2026-10", ["A"])["A"]
    assert (latest.mtd, latest.ftd) == (9, 4)  # the newest reading is the current one


def test_a_falling_month_to_date_figure_is_flagged_but_kept_as_read(csps):
    capture_monthly(parse({"A": {"mtd": 9, "ftd": 1, "target": 7}}), at(10))
    stats = capture_monthly(parse({"A": {"mtd": 6, "ftd": 0, "target": 7}}), at(11))
    assert stats["mtd_decreased"] == 1
    assert pmjdy_monthly.latest_monthly("2026-10", ["A"])["A"].mtd == 6


def test_unknown_codes_are_reported_never_created(csps):
    stats = capture_monthly(
        parse({"A": {"mtd": 1, "ftd": 0, "target": 7}, "NOPE": {"mtd": 3, "ftd": 0, "target": 1}}),
        at(10),
    )
    assert stats["n_unknown_codes"] == 1 and stats["unknown_codes"] == ["NOPE"]
    assert not Csp.objects.filter(csp_code="NOPE").exists()


def test_months_never_mix(csps):
    capture_monthly(
        parse({"A": {"mtd": 30, "ftd": 0, "target": 7}}, "2026-09"),
        dt.datetime(2026, 9, 30, 6, tzinfo=timezone.get_current_timezone()),
    )
    capture_monthly(parse({"A": {"mtd": 2, "ftd": 1, "target": 7}}, "2026-10"), at(2))
    assert monthly_totals("2026-10")["mtd"] == 2  # not 32
    assert monthly_totals("2026-09")["mtd"] == 30
    assert monthly_totals("2026-08") is None  # nothing captured: None, not zero


def test_totals_ignore_missing_cells_instead_of_counting_them_as_zero(csps):
    capture_monthly(
        parse(
            {
                "A": {"mtd": 4, "ftd": None, "target": None},
                "B": {"mtd": None, "ftd": None, "target": None},
            }
        ),
        at(10),
    )
    t = monthly_totals("2026-10")
    assert t["mtd"] == 4 and t["csps_reporting"] == 1 and t["ftd"] is None and t["target"] is None


# ---------------------------------------------------------------- ingest command
class _FakeSheets:
    def __init__(self, grid=None, exc=None):
        self.grid, self.exc, self.calls, self.tab = grid, exc, 0, None

    def service_account(self, filename):
        outer = self

        class _Book:
            def worksheet(self, name):
                outer.tab = name
                return SimpleNamespace(get_all_values=outer._read)

        class _Client:
            def set_timeout(self, t):
                pass

            def open_by_key(self, key):
                return _Book()

        return _Client()

    def _read(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.grid


def _this_month_grid():
    mon, yr = TODAY.strftime("%B"), TODAY.strftime("%y")
    blocks = [
        (f"MTD Achievement {mon}'{yr}", ["PMJJBY", "PMJDY", "Slab"]),
        (f"FTD Achievement {mon}'{yr}", ["PMJDY"]),
        (f"Targets {TODAY:%b} {TODAY.year}", ["PMJDY Monthly"]),
    ]
    return make_grid(blocks, {"A": [[1, 12, "s"], [3], [7]], "B": [[0, 4, "s"], [1], [7]]})


@pytest.fixture
def sheets(monkeypatch, settings):
    settings.GOOGLE_SERVICE_ACCOUNT_FILE = "x.json"
    settings.CALLING_SHEET_ID = "sheet"
    settings.CALLING_SHEET_WORKSHEET_NAME = "Calling Sheet New"
    settings.PMJDY_MIN_INTERVAL_SECONDS = 3600
    fake = _FakeSheets(_this_month_grid())
    monkeypatch.setattr("bankflow.management.commands.ingest_pmjdy.gspread", fake)
    return fake


def test_command_reads_this_months_block_logs_it_and_rate_limits(csps, sheets):
    call_command("ingest_pmjdy")
    assert sheets.tab == "Calling Sheet New"
    assert monthly_totals(MONTH)["mtd"] == 16 and monthly_totals(MONTH)["ftd"] == 4
    log = IngestLog.objects.get(source="pmjdy")
    assert log.status == "success" and log.rows_upserted == 2
    assert "MTD Achievement" in log.file_name  # which block the numbers came from
    call_command("ingest_pmjdy")  # inside the minimum interval: no Sheets call at all
    assert sheets.calls == 1 and IngestLog.objects.filter(source="pmjdy").count() == 1
    call_command("ingest_pmjdy", force=True)
    assert sheets.calls == 2 and PmjdyMonthly.objects.count() == 2  # unchanged: nothing new


def test_command_refuses_when_this_months_block_is_missing(csps, sheets):
    sheets.grid = make_grid([("MTD Achievement January'20", ["PMJDY"])], {"A": [[5]]})
    call_command("ingest_pmjdy")
    log = IngestLog.objects.get(source="pmjdy")
    assert log.status == "failed" and "never reused" in log.error_summary
    assert not PmjdyMonthly.objects.exists()


def test_failed_read_writes_nothing_and_does_not_start_the_rate_limit(csps, sheets):
    sheets.exc = TimeoutError("sheets down")
    call_command("ingest_pmjdy")
    log = IngestLog.objects.get(source="pmjdy")
    assert log.status == "failed" and "TimeoutError" in log.error_summary
    sheets.exc = None
    call_command("ingest_pmjdy")  # the failed attempt does not throttle the retry
    assert sheets.calls == 2 and PmjdyMonthly.objects.count() == 2


# ---------------------------------------------------------------- dashboard
@pytest.fixture
def staff(client):
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    return client


def test_summary_reports_new_accounts_from_the_monthly_block(csps):
    capture_monthly(
        parse(
            {"A": {"mtd": 12, "ftd": 3, "target": 7}, "B": {"mtd": 4, "ftd": 1, "target": 7}}, MONTH
        ),
        timezone.now(),
    )
    na = intelligence.dashboard_summary(*intelligence.resolve_range(30))["new_accounts"]
    assert na["available"] and na["mtd"] == 16 and na["today"] == 4 and na["target"] == 14
    assert na["month_label"] == TODAY.strftime("%B %Y") and na["csps_reporting"] == 2


def test_summary_says_unavailable_not_zero_when_nothing_was_captured(csps):
    na = intelligence.dashboard_summary(*intelligence.resolve_range(30))["new_accounts"]
    assert na["available"] is False and na["mtd"] is None


def test_overview_shows_new_customer_accounts_in_plain_words(staff, csps):
    capture_monthly(
        parse(
            {"A": {"mtd": 12, "ftd": 3, "target": 7}, "B": {"mtd": 4, "ftd": 1, "target": 7}}, MONTH
        ),
        timezone.now(),
    )
    html = staff.get("/dashboard/").content.decode()
    visible = html.split("<main", 1)[1].split("<script", 1)[0]
    flat = " ".join(visible.split())
    assert "New customer accounts" in flat
    assert f"so far in {TODAY.strftime('%B %Y')}" in flat and ">16<" in visible.replace(" ", "")
    assert "4 on the latest day" in flat and "month target 14" in flat
    assert "PMJDY" not in visible  # the agreed wording is "new customer accounts"
    assert "have not been captured yet" not in visible
    detail = staff.get("/dashboard/csp/A/").content.decode()
    assert "12 new this month" in detail  # per CSP, on its own page


def test_overview_is_honest_when_new_accounts_are_missing_or_stale(staff, csps):
    html = staff.get("/dashboard/").content.decode()
    assert f"New customer accounts for {TODAY.strftime('%B %Y')} have not been captured yet" in html
    bump_generation()  # the overview summary is cached
    capture_monthly(
        parse({"A": {"mtd": 1, "ftd": 0, "target": 7}}, MONTH),
        timezone.now() - dt.timedelta(days=4),
    )
    stale = staff.get("/dashboard/").content.decode()
    assert "New customer account numbers were last updated" in stale
