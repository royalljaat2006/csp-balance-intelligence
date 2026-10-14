"""Data progress page + API: when each step ran today and yesterday (IST), what is late, and the
statement files with produced/ingested times. Read-only over what the pipeline already writes."""

import datetime as dt

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from bankflow import progress
from bankflow.models import (
    BaselineSnapshot,
    DailyEstimate,
    HypothesisRun,
    ImportRun,
    PmjdyMonthly,
    StatementFile,
)
from csp.models import Csp, IngestLog
from django.contrib.auth import get_user_model
from django.utils import timezone

pytestmark = pytest.mark.django_db
TZ = timezone.get_current_timezone()
TODAY = timezone.localdate()
YESTERDAY = TODAY - dt.timedelta(days=1)


def at(day, hh, mm=0):
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)


def _csp(code="1A850001"):
    defaults = {"name": code, "account_count": 300}
    return Csp.objects.get_or_create(csp_code=code, defaults=defaults)[0]


def _stamp(obj, **fields):
    """auto_now_add fields can only be back-dated with an UPDATE."""
    type(obj).objects.filter(pk=obj.pk).update(**fields)


def _log(source, when, status="success", error=""):
    log = IngestLog.objects.create(source=source, status=status, error_summary=error)
    _stamp(log, started_at=when, finished_at=when + dt.timedelta(seconds=5))
    return log


def _snapshot(csp, day, when):
    b = BaselineSnapshot.objects.create(csp=csp, snapshot_date=day, avg_balance=1000,
                                        account_count=300)
    _stamp(b, captured_at=when)


def _by_key(data):
    return {s["key"]: s for s in data["steps"]}


NOW = at(TODAY, 14, 30)  # a fixed "now" after the 06:00 snapshot time


# ---------------------------------------------------------------- the function
def test_nothing_has_run_yet_is_reported_as_never_not_as_fine():
    data = progress.pipeline_progress(NOW)
    assert data["today"] == TODAY and data["yesterday"] == YESTERDAY
    assert data["overall"]["state"] == "attention"
    steps = _by_key(data)
    assert steps["calling_sheet"]["health"] == "never"
    assert steps["snapshot"]["health"] == "never"
    assert steps["statements"]["today"]["state"] == "waiting"
    assert steps["snapshot"]["today"]["state"] == "late"  # past 06:00 and nothing captured


def test_snapshot_before_6am_is_waiting_not_late():
    early = at(TODAY, 5, 10)
    data = progress.pipeline_progress(early)
    snap = _by_key(data)["snapshot"]
    assert snap["today"]["state"] == "waiting" and snap["health"] == "waiting"
    assert "6:00 AM" in snap["today"]["summary"]
    assert snap["next_due"] == at(TODAY, 6, 0)


def test_snapshot_captured_today_and_yesterday_shows_both_times():
    c = _csp()
    _snapshot(c, TODAY, at(TODAY, 6, 1))
    _snapshot(c, YESTERDAY, at(YESTERDAY, 6, 0))
    snap = _by_key(progress.pipeline_progress(NOW))["snapshot"]
    assert snap["today"]["state"] == "done" and snap["today"]["first_at"] == at(TODAY, 6, 1)
    assert "06:01" in snap["today"]["summary"] and "1 CSPs" in snap["today"]["summary"]
    assert snap["yesterday"]["first_at"] == at(YESTERDAY, 6, 0)
    assert snap["health"] == "on_time"
    assert snap["next_due"] == at(TODAY + dt.timedelta(days=1), 6, 0)


def test_a_snapshot_taken_hours_late_says_so():
    _snapshot(_csp(), TODAY, at(TODAY, 11, 40))
    snap = _by_key(progress.pipeline_progress(NOW))["snapshot"]
    assert snap["today"]["late"] is True and "later than planned" in snap["today"]["summary"]


def test_statement_ingestion_time_is_reported_with_the_file_time():
    f = StatementFile.objects.create(
        file_hash="a" * 64, filename="statement_1556_13-10-2026.xls", status="imported",
        row_count=12345, last_row_date=YESTERDAY, file_modified_at=at(TODAY, 11, 2))
    _stamp(f, imported_at=at(TODAY, 11, 35))
    run = ImportRun.objects.create(files_seen=1, files_imported=1)
    _stamp(run, started_at=at(TODAY, 11, 35))
    data = progress.pipeline_progress(NOW)
    st = _by_key(data)["statements"]
    assert st["today"]["state"] == "done" and st["today"]["count"] == 1
    assert st["today"]["last_at"] == at(TODAY, 11, 35)
    assert "12,345 transactions" in st["today"]["summary"]
    row = data["statement_files"][0]
    assert row["ingested_at"] == at(TODAY, 11, 35) and row["produced_at"] == at(TODAY, 11, 2)
    assert row["statement_day"] == YESTERDAY
    assert any("ingested (12,345" in e["text"] for e in data["timeline"]["today"])


def test_a_stale_statement_day_is_called_out():
    StatementFile.objects.create(file_hash="b" * 64, filename="s.xls", status="imported",
                                 last_row_date=TODAY - dt.timedelta(days=6))
    st = _by_key(progress.pipeline_progress(NOW))["statements"]
    assert "newest statement ingested is for" in st["note"]


def test_hourly_steps_go_late_after_two_missed_cycles():
    _log("pmjdy", NOW - dt.timedelta(minutes=40))
    assert _by_key(progress.pipeline_progress(NOW))["new_accounts"]["health"] == "on_time"
    IngestLog.objects.all().delete()
    _log("pmjdy", NOW - dt.timedelta(hours=5))
    data = progress.pipeline_progress(NOW)
    assert _by_key(data)["new_accounts"]["health"] == "late"
    assert "New accounts" in data["overall"]["late"]


def test_a_failed_run_is_visible_with_its_error_for_the_api():
    _log("calling_sheet", at(TODAY, 9), status="failed", error="sheet unreachable")
    sheet = _by_key(progress.pipeline_progress(NOW))["calling_sheet"]
    assert sheet["today"]["state"] == "failed" and "1 failed" in sheet["today"]["summary"]
    assert sheet["today"]["error"] == "sheet unreachable"


def test_estimates_and_verdict_are_summarised_in_plain_words():
    c = _csp()
    e = DailyEstimate.objects.create(
        csp=c, estimate_date=TODAY, status="INSUFFICIENT_EVIDENCE", method="carry_forward",
        calculated_at=at(TODAY, 14, 5), current_account_count=300, baseline_value=1000,
        estimated_value=1000, reason_code="NO_HISTORY", details={})
    h = HypothesisRun.objects.create(status="INSUFFICIENT_EVIDENCE", detail="x", n_pairs=0,
                                     n_events=0, metrics={}, config={})
    _stamp(h, created_at=at(TODAY, 3, 0))
    steps = _by_key(progress.pipeline_progress(NOW))
    assert "1 not enough history yet" in steps["estimates"]["today"]["summary"]
    assert "14:05" in steps["estimates"]["today"]["summary"]
    assert "not enough history yet" in steps["verdict"]["today"]["summary"]
    assert e.pk and steps["estimates"]["today"]["by_status"] == {"INSUFFICIENT_EVIDENCE": 1}


def test_new_account_checks_count_the_readings_stored():
    c = _csp()
    p = PmjdyMonthly.objects.create(csp=c, month=f"{TODAY:%Y-%m}", mtd=9, ftd=1, target=30,
                                    observed_at=at(TODAY, 13, 0), snapshot_date=TODAY)
    _log("pmjdy", at(TODAY, 13, 0))
    st = _by_key(progress.pipeline_progress(NOW))["new_accounts"]
    assert st["today"]["readings"] == 1 and "1 new readings stored" in st["today"]["summary"]
    assert p.pk


def test_times_are_india_time_not_utc():
    # 06:00 IST is 00:30 UTC: the step must be matched on the IST day
    c = _csp()
    _snapshot(c, TODAY, at(TODAY, 0, 40))
    snap = _by_key(progress.pipeline_progress(NOW))["snapshot"]
    assert snap["today"]["state"] == "done"
    assert "12:40 AM" not in snap["today"]["summary"] and "00:40" in snap["today"]["summary"]


# ---------------------------------------------------------------- the page
@pytest.fixture
def staff(client):
    u = get_user_model().objects.create_user("st", password="x", is_staff=True)
    client.force_login(u)
    return client


@pytest.fixture
def viewer(client):
    u = get_user_model().objects.create_user("vw", password="x")
    client.force_login(u)
    return client


def test_page_renders_for_everyone_signed_in_and_needs_login(client, staff):
    html = staff.get("/dashboard/progress/").content.decode()
    assert "Data progress" in html and "Morning snapshot" in html
    assert 'data-step="statements"' in html and "Bank statement files" in html
    client.logout()
    assert client.get("/dashboard/progress/").status_code == 302


def test_page_shows_snapshot_and_ingestion_times(staff):
    _snapshot(_csp(), TODAY, timezone.now() - dt.timedelta(minutes=1))
    f = StatementFile.objects.create(file_hash="c" * 64, filename="s.xls", status="imported",
                                     row_count=77, last_row_date=YESTERDAY,
                                     file_modified_at=timezone.now())
    assert f.pk
    html = staff.get("/dashboard/progress/").content.decode()
    assert "Captured at" in html
    assert "Bank statement files" in html and ">77<" in html.replace(" ", "")


def test_error_text_is_for_staff_only(client):
    _log("calling_sheet", timezone.now() - dt.timedelta(minutes=2), status="failed",
         error="secret-detail-xyz")
    u = get_user_model().objects.create_user("vw2", password="x")
    client.force_login(u)
    assert "secret-detail-xyz" not in client.get("/dashboard/progress/").content.decode()
    s = get_user_model().objects.create_user("st2", password="x", is_staff=True)
    client.force_login(s)
    assert "secret-detail-xyz" in client.get("/dashboard/progress/").content.decode()


def test_the_tab_is_in_the_money_section_and_more_hub_and_the_freshness_pill_links_there(viewer):
    html = viewer.get("/dashboard/money/").content.decode()
    assert 'href="/dashboard/progress/"' in html and "Data progress" in html
    more = viewer.get("/dashboard/more/").content.decode()
    assert "<strong>Data progress</strong>" in more


# ---------------------------------------------------------------- the API
@pytest.fixture
def api_key(db):
    def make(scopes):
        consumer = ApiConsumer.objects.create(name=f"t-{scopes}")
        raw = generate_raw_key()
        ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                              scopes=scopes)
        return raw

    return make


def test_api_progress_needs_the_scope_and_returns_the_steps(client, api_key):
    url = "/api/v1/bank-flow/progress"
    assert client.get(url).status_code == 401
    assert client.get(url, HTTP_X_API_KEY=api_key("csp:read")).status_code == 403
    ok = client.get(url, HTTP_X_API_KEY=api_key("report:read"))
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert [s["key"] for s in data["steps"]][:2] == ["calling_sheet", "snapshot"]
    assert data["today"] == TODAY.isoformat() and "timeline" in data
