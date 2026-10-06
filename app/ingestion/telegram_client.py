"""
Thin wrapper around the Telegram Bot API (https://core.telegram.org/bots/api)
— getUpdates (long-poll for new messages), getFile (resolve a file_id to a
download path), and downloading the actual bytes. Deliberately dumb: no
business logic here, no chat_id allow-list decision, no file validation —
those live in manage.py poll_telegram, which is the only caller. This module
just knows how to talk to Telegram's HTTP API.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import requests

_API_ROOT = "https://api.telegram.org"
_TIMEOUT_SECONDS = 30
# getUpdates *can* long-poll server-side, but manage.py poll_telegram is one
# schedule among several inside watch_transactions' single-threaded loop
# (see that command's docstring) — a long block here would stall the other
# schedules sharing the same loop tick. Default to an immediate, non-blocking
# check instead; --telegram-interval provides the "near-live" responsiveness
# via poll frequency, not via blocking on this call.
_DEFAULT_POLL_TIMEOUT_SECONDS = 0


class TelegramApiError(RuntimeError):
    """Raised when Telegram's API responds with ok: false, or the HTTP call
    itself fails — always carries a human-readable reason."""


@dataclasses.dataclass(frozen=True)
class TelegramDocument:
    """One document attachment from a getUpdates message — just the fields
    poll_telegram needs to decide whether to download it."""

    update_id: int
    message_id: int
    chat_id: str
    file_id: str
    file_name: str
    file_unique_id: str  # Telegram's own content-addressed ID — same file, same ID, even re-sent


def _get(token: str, method: str, params: dict | None = None) -> dict | list[dict]:
    url = f"{_API_ROOT}/bot{token}/{method}"
    try:
        response = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramApiError(f"{method} request failed: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramApiError(f"{method} rejected: {payload.get('description', payload)}")
    return payload["result"]


def _get_dict(token: str, method: str, params: dict | None = None) -> dict:
    result = _get(token, method, params)
    if not isinstance(result, dict):
        raise TelegramApiError(f"{method}: expected an object result, got {type(result).__name__}")
    return result


def get_updates(
    token: str, *, offset: int | None = None, timeout: int = _DEFAULT_POLL_TIMEOUT_SECONDS
) -> list[dict]:
    """Raw getUpdates results, oldest first. `offset` should be the highest
    update_id already processed + 1 (Telegram's own ack-by-offset
    convention) — passing it tells Telegram it can stop returning updates
    we've already handled. `timeout` is Telegram's own server-side long-poll
    window in seconds (0 = return immediately)."""
    params: dict = {"timeout": timeout, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    result = _get(token, "getUpdates", params)
    return result if isinstance(result, list) else []


def extract_documents(updates: list[dict]) -> list[TelegramDocument]:
    """Pulls out only the updates that are a message with a document
    attachment — anything else (text-only messages, edited messages,
    non-document media) is silently not a candidate for ingestion, not an
    error."""
    documents = []
    for update in updates:
        message = update.get("message")
        if not message:
            continue
        document = message.get("document")
        if not document:
            continue
        documents.append(
            TelegramDocument(
                update_id=update["update_id"],
                message_id=message["message_id"],
                chat_id=str(message["chat"]["id"]),
                file_id=document["file_id"],
                file_name=(
                    document.get("file_name") or f"telegram-{document['file_unique_id']}.xlsx"
                ),
                file_unique_id=document["file_unique_id"],
            )
        )
    return documents


def download_file(token: str, file_id: str, dest: Path) -> None:
    """Resolves file_id -> Telegram's file_path, then downloads the bytes
    to `dest`. Telegram's own 20MB bot-API file-size cap applies upstream
    of this — a file larger than that never reaches getFile successfully."""
    file_info = _get_dict(token, "getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    url = f"{_API_ROOT}/file/bot{token}/{file_path}"
    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise TelegramApiError(f"file download failed: {exc}") from exc
    dest.write_bytes(response.content)


def get_me(token: str) -> dict:
    """Identity check — used only for a manual `--verify` sanity check, not
    on the hot polling path."""
    return _get_dict(token, "getMe")
