"""
poll_telegram — the chat_id allow-list, dedup, and offset-tracking rules
that sit in front of ingest_transaction_file() for Telegram-delivered
files. telegram_client itself is mocked throughout; these tests are about
this command's own decisions (authorize / skip / dedupe / download / hand
off), not the Telegram HTTP wire format.
"""

from unittest.mock import patch

import openpyxl
import pytest
from csp.models import IngestLog, Transaction
from django.core.management import call_command
from django.test import override_settings
from ingestion import telegram_client

ALLOWED_CHAT = "111222333"

HEADER = [
    "KO ID", "Transaction Date & Time", "Reference Number", "Type of Transaction",
    "From Account", "To Account", "Amount", "Status", "New Date", "CSP Code New",
]


def _make_workbook_bytes(tmp_path, ref="REF900"):
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Success"
    sheet.append(HEADER)
    sheet.append(
        ["1A850001", "01-09-2026 08:00:00 AM", ref, "AEPS ONUS Withdrawal",
         "XXXXXX1", "XXXXXX2", "2000", "Success", "01-09-2026", "1A850001"]
    )
    path = tmp_path / "src.xlsx"
    wb.save(path)
    return path.read_bytes()


def _update(
    update_id, *, chat_id=ALLOWED_CHAT, file_id="F1", file_unique_id="U1", file_name="Txn.xlsx"
):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": int(chat_id)},
            "document": {
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_name": file_name,
            },
        },
    }


@pytest.fixture
def transaction_dir(tmp_path, settings):
    settings.TRANSACTION_DATA_DIR = str(tmp_path)
    return tmp_path


@pytest.mark.django_db
def test_skips_entirely_when_token_not_configured(transaction_dir):
    with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with patch("ingestion.telegram_client.get_updates") as mock_updates:
            call_command("poll_telegram")
    mock_updates.assert_not_called()
    assert not IngestLog.objects.exists()


@pytest.mark.django_db
def test_raises_if_token_set_but_no_allowed_chat_id(transaction_dir):
    from django.core.management.base import CommandError

    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=""):
        with pytest.raises(CommandError):
            call_command("poll_telegram")


@pytest.mark.django_db
def test_downloads_and_ingests_authorized_document(transaction_dir):
    file_bytes = _make_workbook_bytes(transaction_dir)

    def fake_download(token, file_id, dest):
        dest.write_bytes(file_bytes)

    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with (
            patch("ingestion.telegram_client.get_updates", return_value=[_update(1)]),
            patch("ingestion.telegram_client.download_file", side_effect=fake_download),
        ):
            call_command("poll_telegram")

    assert Transaction.objects.filter(ref_number="REF900").exists()
    log = IngestLog.objects.get(source="telegram", external_ref="1")
    assert log.status in (IngestLog.Status.SUCCESS, IngestLog.Status.PARTIAL)
    assert log.source_hash == "U1"


@pytest.mark.django_db
def test_unauthorized_chat_is_never_downloaded(transaction_dir):
    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with (
            patch(
                "ingestion.telegram_client.get_updates",
                return_value=[_update(1, chat_id="999999999")],
            ),
            patch("ingestion.telegram_client.download_file") as mock_download,
        ):
            call_command("poll_telegram")

    mock_download.assert_not_called()
    log = IngestLog.objects.get(source="telegram", external_ref="1")
    assert log.status == IngestLog.Status.INVALID_SOURCE
    assert "Unauthorized" in log.error_summary


@pytest.mark.django_db
def test_non_document_update_is_logged_and_skipped(transaction_dir):
    update = {
        "update_id": 5,
        "message": {"message_id": 5, "chat": {"id": int(ALLOWED_CHAT)}, "text": "hi"},
    }
    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with (
            patch("ingestion.telegram_client.get_updates", return_value=[update]),
            patch("ingestion.telegram_client.download_file") as mock_download,
        ):
            call_command("poll_telegram")

    mock_download.assert_not_called()
    log = IngestLog.objects.get(source="telegram", external_ref="5")
    assert log.status == IngestLog.Status.INVALID_SOURCE


@pytest.mark.django_db
def test_duplicate_file_unique_id_is_skipped_without_download(transaction_dir):
    IngestLog.objects.create(
        source="telegram", file_name="earlier.xlsx", external_ref="1",
        source_hash="U1", status=IngestLog.Status.SUCCESS,
    )
    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with (
            patch(
                "ingestion.telegram_client.get_updates",
                return_value=[_update(2, file_unique_id="U1")],
            ),
            patch("ingestion.telegram_client.download_file") as mock_download,
        ):
            call_command("poll_telegram")

    mock_download.assert_not_called()
    log = IngestLog.objects.get(source="telegram", external_ref="2")
    assert log.status == IngestLog.Status.DUPLICATE_SKIPPED


@pytest.mark.django_db
def test_download_failure_is_logged_as_failed(transaction_dir):
    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with (
            patch("ingestion.telegram_client.get_updates", return_value=[_update(3)]),
            patch(
                "ingestion.telegram_client.download_file",
                side_effect=telegram_client.TelegramApiError("network blip"),
            ),
        ):
            call_command("poll_telegram")

    log = IngestLog.objects.get(source="telegram", external_ref="3")
    assert log.status == IngestLog.Status.FAILED
    assert "network blip" in log.error_summary


@pytest.mark.django_db
def test_offset_advances_past_highest_seen_update_id(transaction_dir):
    IngestLog.objects.create(
        source="telegram", file_name="", external_ref="10",
        status=IngestLog.Status.INVALID_SOURCE,
    )
    with override_settings(TELEGRAM_BOT_TOKEN="TOKEN", TELEGRAM_ALLOWED_CHAT_ID=ALLOWED_CHAT):
        with patch("ingestion.telegram_client.get_updates", return_value=[]) as mock_updates:
            call_command("poll_telegram")

    assert mock_updates.call_args.kwargs["offset"] == 11
