"""
manage.py poll_telegram — Telegram-based transaction-sheet ingestion
(2026-09-18 platform extension), the "send it to the bot in a private
chat" delivery method.

Degrades to a no-op if TELEGRAM_BOT_TOKEN isn't configured — same pattern
as autopilot/nemotron_client.py's NEMOTRON_API_KEY — since this is an
additive delivery channel, not something the core pipeline depends on.
Unlike a blank token, a set token with no TELEGRAM_CHAT_ID allow-list is a
misconfiguration and fails loudly (CommandError): without an allow-list any
chat that discovers the bot could submit files.

Every downloaded document is handed to ingestion/file_ingest.py's
ingest_transaction_file() — the exact same validate/parse/upsert/file
pipeline manage.py ingest_transactions uses for folder-watch files. This
command's own job is narrowly: talk to Telegram, enforce the chat_id
allow-list, dedupe, and download — never re-parse or re-validate a
transaction sheet itself.

Every Telegram update (document or not, authorized or not, duplicate or
not) gets an IngestLog row, because update_id-based offset tracking (so a
handled update is never re-fetched from Telegram) is derived from
IngestLog itself — see _next_offset() below — rather than a second,
Telegram-specific state table.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import structlog
from csp.models import IngestLog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ingestion import telegram_client
from ingestion.file_ingest import ingest_transaction_file
from ingestion.file_safety import IngestionPaths

logger = structlog.get_logger("ingestion")


def _next_offset() -> int | None:
    """One past the highest update_id we've already recorded an IngestLog
    for — Telegram's own ack-by-offset convention. None (Telegram's
    "from the beginning") only on a completely fresh bot with no history."""
    last = (
        IngestLog.objects.filter(source="telegram")
        .exclude(external_ref="")
        .order_by("-id")
        .first()
    )
    if last is None:
        return None
    return int(last.external_ref) + 1


def _safe_filename(name: str) -> str:
    """Telegram-supplied file names are attacker/user-controlled input —
    never trusted as a path. Keep only the basename, and timestamp-prefix
    it so two documents in the same poll can't collide in incoming/ before
    ingest_transaction_file() files the first one away."""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return f"telegram_{stamp}_{Path(name).name}"


def _log_skip(
    *, file_name: str, update_id: int, status: str, reason: str, source_hash: str = ""
) -> None:
    IngestLog.objects.create(
        source="telegram",
        file_name=file_name,
        external_ref=str(update_id),
        source_hash=source_hash,
        status=status,
        error_summary=reason,
        finished_at=timezone.now(),
    )


class Command(BaseCommand):
    help = "Poll Telegram for transaction-sheet documents from the allow-listed chat."

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stdout.write("TELEGRAM_BOT_TOKEN not configured — skipping Telegram poll.")
            return

        allowed_chat_id = settings.TELEGRAM_ALLOWED_CHAT_ID
        if not allowed_chat_id:
            raise CommandError(
                "TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is not — refusing to poll "
                "without a chat allow-list (any chat could otherwise submit files)."
            )

        try:
            updates = telegram_client.get_updates(token, offset=_next_offset())
        except telegram_client.TelegramApiError as exc:
            logger.exception("poll_failed", stage="TELEGRAM", reason=str(exc))
            self.stderr.write(f"Telegram poll failed: {exc}")
            return

        if not updates:
            self.stdout.write("No new Telegram updates.")
            return

        paths = IngestionPaths(root=Path(settings.TRANSACTION_DATA_DIR))
        paths.ensure_exist()

        for update in updates:
            self._handle_update(update, token, allowed_chat_id, paths)

    def _handle_update(
        self, update: dict, token: str, allowed_chat_id: str, paths: IngestionPaths
    ) -> None:
        update_id = update["update_id"]
        message = update.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        document = message.get("document")

        if not document:
            _log_skip(
                file_name="",
                update_id=update_id,
                status=IngestLog.Status.INVALID_SOURCE,
                reason="Update has no document attachment.",
            )
            return

        file_name = document.get("file_name") or f"telegram-{document['file_unique_id']}.xlsx"

        if chat_id != allowed_chat_id:
            _log_skip(
                file_name=file_name,
                update_id=update_id,
                status=IngestLog.Status.INVALID_SOURCE,
                reason=f"Unauthorized chat_id {chat_id!r} — file ignored.",
            )
            logger.warning(
                "unauthorized_chat", stage="TELEGRAM", chat_id=chat_id, update_id=update_id
            )
            return

        file_unique_id = document["file_unique_id"]
        if IngestLog.objects.filter(source="telegram", source_hash=file_unique_id).exists():
            _log_skip(
                file_name=file_name,
                update_id=update_id,
                status=IngestLog.Status.DUPLICATE_SKIPPED,
                reason="This exact file was already ingested via Telegram — skipped.",
                source_hash=file_unique_id,
            )
            return

        dest = paths.incoming / _safe_filename(file_name)
        try:
            telegram_client.download_file(token, document["file_id"], dest)
        except telegram_client.TelegramApiError as exc:
            _log_skip(
                file_name=file_name,
                update_id=update_id,
                status=IngestLog.Status.FAILED,
                reason=f"Download failed: {exc}",
                source_hash=file_unique_id,
            )
            logger.exception("download_failed", stage="TELEGRAM", update_id=update_id)
            return

        log = ingest_transaction_file(
            paths,
            dest,
            source="telegram",
            external_ref=str(update_id),
            source_hash=file_unique_id,
        )
        self.stdout.write(
            f"{file_name} (Telegram): {log.status}, {log.rows_upserted} upserted, "
            f"{log.rows_rejected} rejected."
        )
