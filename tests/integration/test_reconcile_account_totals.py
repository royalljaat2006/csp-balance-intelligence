"""reconcile_account_totals: read only, and it names every row that explains a gap between the
Calling Sheet's Total Accounts and the dashboard. Invented codes and counts."""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from csp.models import Csp, DailyBalance, IngestLog
from django.core.management import call_command
from django.test import override_settings
from ingestion.calling_sheet_ingest import REQUIRED_COLUMNS

pytestmark = pytest.mark.django_db


def _client(rows):
    header = list(REQUIRED_COLUMNS)
    data = [["section"], header] + [
        [c, "name", "a@x.in", "9876543210", n, "₹ 100", "₹ 50", ""] for c, n in rows]
    client = MagicMock()
    client.open_by_key.return_value.worksheet.return_value.get_all_values.return_value = data
    return client


def _run(command, rows):
    out = StringIO()
    with override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="f.json", CALLING_SHEET_ID="id"), patch(
            "gspread.service_account", return_value=_client(rows)), patch(
            "ingestion.management.commands.ingest_calling_sheet.gspread.service_account",
            return_value=_client(rows)):
        call_command(command, stdout=out)
    return out.getvalue()


def test_it_names_the_rows_that_explain_the_gap_and_changes_nothing():
    _run("ingest_calling_sheet", [("1A850001", "300"), ("1A850002", "250"), ("1A850003", "400")])
    logs = IngestLog.objects.count()
    balances = list(DailyBalance.objects.order_by("id").values_list(
        "csp_id", "account_count", "daily_avg_balance"))
    sheet_now = [("1A850001", "310"),        # changed in the sheet, not ingested yet
                 ("1A850002", ""),           # blank cell: the ingest keeps the old count
                 ("1A850003", "400"),
                 ("f", "1190"),             # invalid CSP ID: ignored by the ingest
                 ("", "7")]                 # a count with no CSP ID
    text = _run("reconcile_account_totals", sheet_now)
    # 310 + 400 + 1190 + 7: the blank cell adds nothing
    assert "Sheet column sum (every row):" in text and "1,907" in text
    assert "Dashboard total of customer accounts today:" in text and "950" in text
    assert "'f': 1190" in text
    assert "1A850001: sheet 310, system 300" in text and "not ingested yet" in text
    assert "1A850002: sheet blank, system 250" in text and "keeps the previous count" in text
    assert "(1 rows)" in text and "Nothing was changed." in text
    # read only: no log row, no balance, no CSP touched
    assert IngestLog.objects.count() == logs
    assert balances == list(DailyBalance.objects.order_by("id").values_list(
        "csp_id", "account_count", "daily_avg_balance"))
    assert Csp.objects.get(csp_code="1A850001").account_count == 300


def test_when_everything_matches_it_says_so():
    rows = [("1A850001", "300"), ("1A850002", "250")]
    _run("ingest_calling_sheet", rows)
    text = _run("reconcile_account_totals", rows)
    assert "Every valid CSP's count in the sheet equals the count the dashboard uses." in text
    assert "+0" in text
