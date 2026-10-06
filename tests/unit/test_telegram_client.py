"""
telegram_client — pure HTTP-wrapper behaviour: ok/not-ok response handling,
document extraction from raw updates, and file download. All requests are
mocked; no real network calls.
"""

from unittest.mock import Mock, patch

import pytest
from ingestion import telegram_client


def _response(payload):
    mock = Mock()
    mock.json.return_value = payload
    mock.raise_for_status = Mock()
    return mock


@patch("ingestion.telegram_client.requests.get")
def test_get_updates_returns_result_list(mock_get):
    mock_get.return_value = _response({"ok": True, "result": [{"update_id": 1}]})
    updates = telegram_client.get_updates("TOKEN")
    assert updates == [{"update_id": 1}]


@patch("ingestion.telegram_client.requests.get")
def test_get_updates_passes_offset(mock_get):
    mock_get.return_value = _response({"ok": True, "result": []})
    telegram_client.get_updates("TOKEN", offset=42)
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["offset"] == 42


@patch("ingestion.telegram_client.requests.get")
def test_api_rejects_raises_telegram_api_error(mock_get):
    mock_get.return_value = _response({"ok": False, "description": "Unauthorized"})
    with pytest.raises(telegram_client.TelegramApiError, match="Unauthorized"):
        telegram_client.get_updates("BAD_TOKEN")


@patch("ingestion.telegram_client.requests.get")
def test_network_failure_raises_telegram_api_error(mock_get):
    import requests

    mock_get.side_effect = requests.ConnectionError("boom")
    with pytest.raises(telegram_client.TelegramApiError):
        telegram_client.get_updates("TOKEN")


def test_extract_documents_skips_non_document_messages():
    updates = [
        {"update_id": 1, "message": {"message_id": 10, "chat": {"id": 5}, "text": "hi"}},
        {"update_id": 2, "message": {"message_id": 11, "chat": {"id": 5}}},  # no document key
        {"update_id": 3},  # no message at all (e.g. edited_message)
    ]
    assert telegram_client.extract_documents(updates) == []


def test_extract_documents_pulls_out_document_fields():
    updates = [
        {
            "update_id": 7,
            "message": {
                "message_id": 99,
                "chat": {"id": 12345},
                "document": {
                    "file_id": "FILE_ID_1",
                    "file_unique_id": "UNIQUE_1",
                    "file_name": "Transactions Sep.xlsx",
                },
            },
        }
    ]
    docs = telegram_client.extract_documents(updates)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.update_id == 7
    assert doc.chat_id == "12345"
    assert doc.file_id == "FILE_ID_1"
    assert doc.file_unique_id == "UNIQUE_1"
    assert doc.file_name == "Transactions Sep.xlsx"


def test_extract_documents_falls_back_to_generated_name():
    updates = [
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 1},
                "document": {"file_id": "F", "file_unique_id": "U1"},
            },
        }
    ]
    doc = telegram_client.extract_documents(updates)[0]
    assert doc.file_name == "telegram-U1.xlsx"


@patch("ingestion.telegram_client.requests.get")
def test_download_file_resolves_path_then_writes_bytes(mock_get, tmp_path):
    file_info_response = _response({"ok": True, "result": {"file_path": "docs/file_1.xlsx"}})
    content_response = Mock()
    content_response.content = b"workbook-bytes"
    content_response.raise_for_status = Mock()
    mock_get.side_effect = [file_info_response, content_response]

    dest = tmp_path / "downloaded.xlsx"
    telegram_client.download_file("TOKEN", "FILE_ID", dest)

    assert dest.read_bytes() == b"workbook-bytes"
    first_call_url = mock_get.call_args_list[0].args[0]
    second_call_url = mock_get.call_args_list[1].args[0]
    assert "getFile" in first_call_url
    assert "docs/file_1.xlsx" in second_call_url
