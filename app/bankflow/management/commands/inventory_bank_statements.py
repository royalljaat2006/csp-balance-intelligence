"""
manage.py inventory_bank_statements -- READ-ONLY inventory of a statement folder.

For every statement-looking file it reports: path, size, modified time, SHA-256
(streamed), the REAL format (sniffed from the bytes, never the extension), and --
for tab-delimited statements -- account suffix and reporting period from the
header. Only the first 64 KB of each file is inspected for the header.

It never creates, moves, renames, locks or deletes anything in the scanned
folder. The optional --csv report is written only where the operator points it
and is refused if that location is inside the scanned folder. Processing status
comes from the Bank-Flow tables when a database is available (--no-db skips it).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bankflow.ingest import discover_files
from bankflow.parser import peek_header

PEEK_BYTES = 64 * 1024
COLUMNS = [
    "path",
    "size_bytes",
    "modified",
    "sha256",
    "format",
    "account_suffix",
    "period_start",
    "period_end",
    "opening_balance",
    "status",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Command(BaseCommand):
    help = "Read-only inventory of bank statement files (real format, period, processing status)."

    def add_arguments(self, parser):
        parser.add_argument("--path", help="Folder or file (default: BANKFLOW_WATCH_DIR).")
        parser.add_argument("--recursive", action="store_true")
        parser.add_argument(
            "--pattern", default=None, help="Only files whose NAME matches this glob."
        )
        parser.add_argument(
            "--no-db", action="store_true", help="Do not look up processing status."
        )
        parser.add_argument(
            "--csv", help="Write the inventory to this CSV (must be outside the scanned folder)."
        )

    def handle(self, *args, **opts):
        root = Path(opts["path"] or settings.BANKFLOW_WATCH_DIR)
        if not root.exists():
            raise CommandError(f"Path does not exist or is not readable: {root}")
        if opts["csv"]:
            out = Path(opts["csv"]).resolve()
            base = root.resolve() if root.is_dir() else root.resolve().parent
            if base == out.parent or base in out.parents:
                raise CommandError("Refusing to write the CSV inside the scanned folder.")

        # None  -> status lookup skipped; dict -> hash -> StatementFile.status
        statuses: dict[str, str] | None = None
        if not opts["no_db"]:
            try:
                from bankflow.models import StatementFile

                statuses = dict(StatementFile.objects.values_list("file_hash", "status"))
            except Exception:  # noqa: BLE001 -- inventory must work with no database at all
                self.stderr.write("(database unavailable: processing status not shown)")

        rows = []
        for f in discover_files(root, recursive=opts["recursive"], pattern=opts["pattern"]):
            st = f.stat()
            with f.open("rb") as fh:
                head = fh.read(PEEK_BYTES)
            info = peek_header(head)
            digest = _sha256(f)
            if statuses is None:
                status = "unknown"
            else:
                status = statuses.get(digest, "not_processed")
            rows.append(
                {
                    "path": str(f),
                    "size_bytes": st.st_size,
                    "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "sha256": digest,
                    "format": info.detected_format,
                    "account_suffix": info.account_number[-5:] if info.account_number else "",
                    "period_start": info.start_date or "",
                    "period_end": info.end_date or "",
                    "opening_balance": (
                        info.opening_balance if info.opening_balance is not None else ""
                    ),
                    "status": status,
                }
            )

        self.stdout.write(f"{len(rows)} statement-looking file(s) under {root}")
        for r in rows:
            self.stdout.write(
                f"{r['modified']}  {r['size_bytes']:>12,}  {r['format']:<10} "
                f"acct=...{r['account_suffix'] or '?':<5} "
                f"{r['period_start'] or '?'}..{r['period_end'] or '?'}  "
                f"{r['status']:<13} {r['path']}"
            )
        if opts["csv"]:
            with open(opts["csv"], "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=COLUMNS)
                w.writeheader()
                w.writerows(rows)
            self.stdout.write(f"CSV written: {opts['csv']}")
