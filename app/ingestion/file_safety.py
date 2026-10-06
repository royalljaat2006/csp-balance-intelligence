"""
File-safety helpers for the watched-folder workflow (Phase 4 / Phase 10):

    <TRANSACTION_DATA_DIR>/incoming/   — the watched folder
    <TRANSACTION_DATA_DIR>/processed/  — moved here after successful ingest
    <TRANSACTION_DATA_DIR>/failed/     — moved here on rejection, never deleted

Source files are always *moved*, never deleted — a rejected file is evidence
someone needs to look at, not garbage.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IngestionPaths:
    root: Path

    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def processed(self) -> Path:
        return self.root / "processed"

    @property
    def failed(self) -> Path:
        return self.root / "failed"

    def ensure_exist(self) -> None:
        for d in (self.incoming, self.processed, self.failed):
            d.mkdir(parents=True, exist_ok=True)


def _timestamped_destination(dest_dir: Path, source: Path) -> Path:
    """Avoid clobbering an existing file of the same name in processed/failed."""
    import datetime as dt

    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    return dest_dir / f"{source.stem}.{stamp}{source.suffix}"


def move_to_processed(paths: IngestionPaths, source: Path) -> Path:
    dest = _timestamped_destination(paths.processed, source)
    shutil.move(str(source), str(dest))
    return dest


def move_to_failed(paths: IngestionPaths, source: Path) -> Path:
    dest = _timestamped_destination(paths.failed, source)
    shutil.move(str(source), str(dest))
    return dest
