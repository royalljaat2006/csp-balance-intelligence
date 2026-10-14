"""
The fast-loading work: gzip for pages (not for the live stream), the calling-sheet poll that leaves
the cache alone when nothing changed, and the cache warm-up the bank-flow worker runs.
"""

from unittest.mock import MagicMock, patch

import pytest
from common.cache import cache_peek, get_generation
from common.middleware import CompressMiddleware
from csp.models import Csp, IngestLog
from dashboard import shell, warm
from django.core.management import call_command
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, override_settings
from django.utils import timezone
from ingestion.calling_sheet_ingest import REQUIRED_COLUMNS

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- compression
def _request():
    return RequestFactory().get("/", HTTP_ACCEPT_ENCODING="gzip")


def test_a_big_page_is_gzipped_for_a_browser_that_accepts_it():
    mw = CompressMiddleware(lambda r: None)
    resp = mw.process_response(_request(), HttpResponse("<p>row</p>" * 5000))
    assert resp["Content-Encoding"] == "gzip"
    assert len(resp.content) < 5000


def test_a_client_that_does_not_ask_gets_the_plain_body():
    mw = CompressMiddleware(lambda r: None)
    request = RequestFactory().get("/")
    resp = mw.process_response(request, HttpResponse("<p>row</p>" * 5000))
    assert "Content-Encoding" not in resp


def test_the_live_event_stream_is_never_compressed_or_buffered():
    mw = CompressMiddleware(lambda r: None)
    resp = StreamingHttpResponse(iter([b"data: 1\n\n"]), content_type="text/event-stream")
    out = mw.process_response(_request(), resp)
    assert "Content-Encoding" not in out


# ---------------------------------------------------------------- calling-sheet poll
def _sheet(accounts: str):
    header = list(REQUIRED_COLUMNS)
    row = ["1A850001", "Asha", "a@x.in", "9876543210", accounts, "₹ 100", "₹ 50", ""]
    client = MagicMock()
    client.open_by_key.return_value.worksheet.return_value.get_all_values.return_value = [
        ["section"], header, row]
    return client


def _poll(client):
    with (
        override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="fake.json", CALLING_SHEET_ID="id"),
        patch("ingestion.management.commands.ingest_calling_sheet.gspread.service_account",
              return_value=client),
    ):
        call_command("ingest_calling_sheet")


def test_an_identical_poll_writes_nothing_and_keeps_the_cache():
    _poll(_sheet("100"))
    first = get_generation()
    assert Csp.objects.get(csp_code="1A850001").account_count == 100
    assert first >= 1  # the first poll changed data, so it invalidated the cache

    _poll(_sheet("100"))
    assert get_generation() == first  # nothing changed: cached figures stay valid
    log = IngestLog.objects.filter(source="calling_sheet").latest("started_at")
    assert log.rows_upserted == 0


def test_a_changed_poll_writes_and_invalidates():
    _poll(_sheet("100"))
    first = get_generation()
    _poll(_sheet("101"))
    assert get_generation() == first + 1
    assert Csp.objects.get(csp_code="1A850001").account_count == 101


# ---------------------------------------------------------------- cache warm-up
def test_warm_up_fills_the_shell_so_pages_find_it_ready():
    assert cache_peek(shell.ATTENTION_KEY)[0] is False
    result = warm.warm_dashboard_cache()
    assert result["warmed"] is True
    assert not [v for v in result["steps"].values() if v.startswith("failed")]
    assert cache_peek(shell.ATTENTION_KEY)[0] is True
    assert cache_peek(shell.day_compare_key())[0] is True


def test_warm_up_is_a_no_op_until_the_data_changes_or_the_day_rolls_over():
    assert warm.warm_dashboard_cache()["warmed"] is True
    again = warm.warm_dashboard_cache()
    assert again["warmed"] is False and again["reason"] == "already_warm"
    from common.cache import bump_generation

    bump_generation()
    assert warm.warm_dashboard_cache()["warmed"] is True
    assert warm.warm_dashboard_cache(force=True)["warmed"] is True


def test_one_failing_figure_does_not_stop_the_rest():
    with patch.object(shell, "attention_count", side_effect=RuntimeError("boom")):
        result = warm.warm_dashboard_cache()
    assert result["steps"]["attention_count"] == "failed: RuntimeError"
    assert result["steps"]["day_compare"].endswith("ms")


def test_warm_command_reports_what_it_did(capsys):
    call_command("warm_dashboard_cache")
    assert "warmed in" in capsys.readouterr().out
    call_command("warm_dashboard_cache")
    assert "already warm" in capsys.readouterr().out


def test_the_warm_up_does_not_mark_a_generation_that_was_retired_while_it_ran():
    from common.cache import bump_generation

    def retire_midway():
        bump_generation()
        return None

    with patch.object(shell, "attention_count", side_effect=retire_midway):
        warm.warm_dashboard_cache()
    # the figures computed before the bump are already stale: the next pass must run again
    assert warm.warm_dashboard_cache()["warmed"] is True


def test_today_is_part_of_the_warm_state():
    result = warm.warm_dashboard_cache()
    assert str(timezone.localdate()) in result["state"]
