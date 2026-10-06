"""
manage.py ingest_calling_sheet's Sheets-fetch bounded retry/timeout
(P0 fast-pass, 2026-09-21) — a hung/transiently-failing Google Sheets call
must not block the calling-sheet worker forever, but a permanently broken
one must still fail (and log a real IngestLog failure) within a few
retries, not hang or loop forever.
"""

from unittest.mock import MagicMock, patch

import pytest
from csp.models import IngestLog
from django.core.management import call_command
from django.test import override_settings

from ingestion.calling_sheet_ingest import REQUIRED_COLUMNS

_VALID_HEADER_ROWS = [["section label"], REQUIRED_COLUMNS]


@override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="fake.json", CALLING_SHEET_ID="fake-id")
@pytest.mark.django_db
def test_transient_failure_then_success_is_retried_and_succeeds():
    good_client = MagicMock()
    good_client.set_timeout = MagicMock()
    good_client.open_by_key.return_value.worksheet.return_value.get_all_values.return_value = (
        _VALID_HEADER_ROWS
    )

    with (
        patch(
            "ingestion.management.commands.ingest_calling_sheet.gspread.service_account",
            side_effect=[ConnectionError("timeout"), good_client],
        ),
        patch("ingestion.management.commands.ingest_calling_sheet.time.sleep"),
    ):
        call_command("ingest_calling_sheet")

    log = IngestLog.objects.filter(source="calling_sheet").latest("started_at")
    assert log.status != IngestLog.Status.FAILED


@override_settings(GOOGLE_SERVICE_ACCOUNT_FILE="fake.json", CALLING_SHEET_ID="fake-id")
@pytest.mark.django_db
def test_permanent_failure_exhausts_retries_and_fails_cleanly():
    with (
        patch(
            "ingestion.management.commands.ingest_calling_sheet.gspread.service_account",
            side_effect=PermissionError("bad credentials"),
        ),
        patch("ingestion.management.commands.ingest_calling_sheet.time.sleep") as mock_sleep,
    ):
        call_command("ingest_calling_sheet")

    log = IngestLog.objects.filter(source="calling_sheet").latest("started_at")
    assert log.status == IngestLog.Status.FAILED
    assert "bad credentials" in log.error_summary
    assert mock_sleep.call_count == 2  # bounded: 2 retries, not infinite
