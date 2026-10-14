#!/usr/bin/env python3
"""
Watchdog for the live transaction workbook -- replaces the `*/20` cron around
sync_transaction_sheet.sh. Runs on the HOST (a systemd service), not in a container.

The real transaction data on the R730 is a continuously updated monthly master workbook
("Transaction Oct'26.xlsx") that lives in another team's folder. This process watches that ONE file
and, once it has changed AND stopped changing, copies it into this app's own incoming/ folder,
where the existing `ingest_transactions` job picks it up exactly as it always has.

What it guarantees
  * READ-ONLY on the source: it only stats and reads the workbook; it never writes, renames,
    deletes or chmods anything in the other team's directory.
  * No half-written copies: the file must be unchanged for TRANSACTION_WATCH_SETTLE_SECONDS before
    it is copied, the copy is written to a hidden `.part` file and renamed into place, and it is
    discarded if the source changed while it was being copied. (`ingest_transactions` only looks
    at `*.xlsx`, so it never sees the `.part` file.)
  * Never overwrites a copy the app has not consumed yet (the old cron's `cp` could replace a file
    in the middle of an ingest): it waits, logs, and copies the newest version once incoming/
    is empty.
  * Only copies when something changed, plus a safety re-sync every TRANSACTION_WATCH_RESYNC_HOURS
    (ingest is idempotent), so a lost or failed ingest heals itself.
  * Writes a heartbeat file (TRANSACTION_DATA_DIR_HOST/transaction_watchdog.json) every poll, so
    "is it alive, when did it last copy" can be answered without reading logs.

Standard library only: it runs under the system python3, no virtualenv. See docs/DEPLOYMENT.md.

Usage
  watch_transaction_sheet.py            run forever (what the systemd unit does)
  watch_transaction_sheet.py --once     look once, copy now if the workbook changed (no settle wait)
  watch_transaction_sheet.py --env-file F   read KEY=VALUE settings from F first (same file the
                                        systemd unit uses), for starting it without systemd

Settings (environment; the systemd unit reads them from an EnvironmentFile)
  TRANSACTION_DATA_DIR_HOST         required: the app data dir that holds incoming/ and processed/
  TRANSACTION_SOURCE_DIR            default /home/shumit/sbikisok
  TRANSACTION_WATCH_POLL_SECONDS    default 30
  TRANSACTION_WATCH_SETTLE_SECONDS  default 45
  TRANSACTION_WATCH_RESYNC_HOURS    default 6   (0 disables the safety re-sync)
  TRANSACTION_SYNC_RETENTION_DAYS   default 14  (how long our own synced copies stay in processed/)
  TRANSACTION_WATCH_TZ              default Asia/Kolkata (which month's workbook is "current")
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SYNC_BASENAME = "transaction_sync"
PENDING_WARN_SECONDS = 30 * 60
HEARTBEAT_NAME = "transaction_watchdog.json"


def month_filename(day: dt.date) -> str:
    """The other team's naming, `Transaction Oct'26.xlsx` (English month, whatever the locale)."""
    return f"Transaction {MONTHS[day.month - 1]}'{day:%y}.xlsx"


@dataclass(frozen=True)
class Config:
    source_dir: Path
    data_dir: Path
    poll_seconds: float = 30.0
    settle_seconds: float = 45.0
    resync_seconds: float = 6 * 3600.0
    retention_days: int = 14
    tz: str = "Asia/Kolkata"

    @property
    def incoming(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def processed(self) -> Path:
        return self.data_dir / "processed"

    @property
    def heartbeat(self) -> Path:
        return self.data_dir / HEARTBEAT_NAME

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        data = env.get("TRANSACTION_DATA_DIR_HOST", "").strip()
        if not data:
            raise SystemExit("TRANSACTION_DATA_DIR_HOST must point at the app data directory")
        return cls(
            source_dir=Path(env.get("TRANSACTION_SOURCE_DIR", "/home/shumit/sbikisok")),
            data_dir=Path(data),
            poll_seconds=float(env.get("TRANSACTION_WATCH_POLL_SECONDS", "30")),
            settle_seconds=float(env.get("TRANSACTION_WATCH_SETTLE_SECONDS", "45")),
            resync_seconds=float(env.get("TRANSACTION_WATCH_RESYNC_HOURS", "6")) * 3600,
            retention_days=int(env.get("TRANSACTION_SYNC_RETENTION_DAYS", "14")),
            tz=env.get("TRANSACTION_WATCH_TZ", "Asia/Kolkata"),
        )


class _SourceChangedDuringCopy(Exception):
    pass


class Watcher:
    """One `step()` per poll. The clock is injectable so it can be tested without sleeping."""

    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.clock = clock
        self._log = log or self._stdout
        self.started_at = clock()
        self.state = "starting"
        self.last_error = ""
        self.seen_sig: tuple[int, int] | None = None
        self.changed_at: float | None = None
        self.copied_sig: tuple[int, int] | None = None
        self.last_copy_at: float | None = None
        self.last_copy_bytes: int | None = None
        self.copies = 0
        self.last_poll_at: float | None = None
        self._logged_state = ""
        self._pending_since: float | None = None
        self._pending_warned_at: float | None = None

    # ------------------------------------------------------------ small helpers
    @staticmethod
    def _stdout(msg: str) -> None:
        print(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} "
              f"[watch_transaction_sheet] {msg}", flush=True)

    def source_path(self) -> Path:
        today = dt.datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.tz)).date()
        return self.cfg.source_dir / month_filename(today)

    @property
    def dest(self) -> Path:
        return self.cfg.incoming / f"{SYNC_BASENAME}.xlsx"

    def _set(self, state: str, detail: str = "") -> str:
        self.state = state
        if state != self._logged_state:  # log changes and actions, not every quiet poll
            self._log(f"{state}{': ' + detail if detail else ''}")
            self._logged_state = state
        return state

    # ------------------------------------------------------------ the poll
    def step(self, *, settle_seconds: float | None = None) -> str:
        now = self.clock()
        self.last_poll_at = now
        settle = self.cfg.settle_seconds if settle_seconds is None else settle_seconds
        try:
            state = self._evaluate(now, settle)
            self.last_error = "" if state not in ("error", "source_unreadable") else self.last_error
        except Exception as exc:  # noqa: BLE001 -- one bad poll must never kill the service
            self.last_error = f"{type(exc).__name__}: {exc}"
            state = self._set("error", self.last_error)
        self.write_heartbeat()
        return state

    def _evaluate(self, now: float, settle: float) -> str:
        src = self.source_path()
        try:
            st = src.stat()
        except FileNotFoundError:
            return self._set("waiting_for_source", f"{src} does not exist yet")
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self._set("source_unreadable", self.last_error)

        sig = (st.st_mtime_ns, st.st_size)
        if sig != self.seen_sig:
            self.seen_sig, self.changed_at = sig, now
        assert self.changed_at is not None

        due_resync = (
            self.cfg.resync_seconds > 0 and self.copied_sig == sig
            and self.last_copy_at is not None
            and now - self.last_copy_at >= self.cfg.resync_seconds and not self.dest.exists()
        )
        if sig == self.copied_sig and not due_resync:
            self._pending_since = None
            return self._set("up_to_date")
        if now - self.changed_at < settle:
            return self._set("settling", f"{src.name} changed {now - self.changed_at:.0f}s ago")
        if self.dest.exists():
            return self._waiting_for_ingest(now)
        self._pending_since = None
        try:
            self._copy(src, st)
        except _SourceChangedDuringCopy:
            self.seen_sig = None  # re-detect and settle again
            return self._set("source_changed_during_copy", "copy discarded; will retry")
        self.copied_sig, self.last_copy_at, self.last_copy_bytes = sig, now, st.st_size
        self.copies += 1
        self._prune(now)
        return self._set("copied", f"{src.name} ({st.st_size:,} bytes) -> {self.dest}")

    def _waiting_for_ingest(self, now: float) -> str:
        if self._pending_since is None:
            self._pending_since = now
        waited = now - self._pending_since
        if waited >= PENDING_WARN_SECONDS and (
                self._pending_warned_at is None
                or now - self._pending_warned_at >= PENDING_WARN_SECONDS):
            self._pending_warned_at = now
            self._log(f"WARNING: the previous copy has sat in incoming/ for {waited / 60:.0f} min "
                      "and has not been ingested. Is worker-ingestion running?")
        return self._set("waiting_for_ingest", "the previous copy is still in incoming/")

    # ------------------------------------------------------------ copy / prune / heartbeat
    def _copy(self, src: Path, st: os.stat_result) -> None:
        self.cfg.incoming.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.incoming / f".{SYNC_BASENAME}.xlsx.part"
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while chunk := fin.read(1 << 20):
                    fout.write(chunk)
                fout.flush()
                os.fsync(fout.fileno())
            after = src.stat()
            if (after.st_mtime_ns, after.st_size) != (st.st_mtime_ns, st.st_size) \
                    or tmp.stat().st_size != st.st_size:
                raise _SourceChangedDuringCopy
            os.replace(tmp, self.dest)
        finally:
            tmp.unlink(missing_ok=True)  # only our own temp file; a no-op after the rename

    def _prune(self, now: float) -> None:
        """Our own redundant synced copies in processed/ only (matched by name prefix)."""
        if not self.cfg.processed.is_dir():
            return
        cutoff = now - self.cfg.retention_days * 86400
        for p in self.cfg.processed.glob(f"{SYNC_BASENAME}.*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def write_heartbeat(self) -> None:
        def iso(ts: float | None) -> str | None:
            return None if ts is None else dt.datetime.fromtimestamp(
                ts, ZoneInfo(self.cfg.tz)).isoformat(timespec="seconds")

        try:
            src = self.source_path()
        except Exception:  # noqa: BLE001
            src = self.cfg.source_dir
        beat = {
            "pid": os.getpid(), "state": self.state, "source_file": src.name,
            "started_at": iso(self.started_at), "last_poll_at": iso(self.last_poll_at),
            "last_copy_at": iso(self.last_copy_at), "last_copy_bytes": self.last_copy_bytes,
            "copies_total": self.copies, "poll_seconds": self.cfg.poll_seconds,
            "last_error": self.last_error,
        }
        try:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.heartbeat.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(beat, indent=2), encoding="utf-8")
            os.replace(tmp, self.cfg.heartbeat)
        except OSError as exc:  # a heartbeat that cannot be written must not stop the sync
            self._log(f"could not write the heartbeat: {exc}")


def load_env_file(path: str | Path, env: dict | None = None) -> dict:
    """Read simple KEY=VALUE lines (comments and blanks ignored; real environment variables win)."""
    env = os.environ if env is None else env
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip("\"'"))
    return env


def run_forever(watcher: Watcher) -> int:
    stop = threading.Event()

    def _stop(signum, _frame):
        watcher._log(f"received signal {signum}; stopping")
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    cfg = watcher.cfg
    watcher._log(f"started: watching {cfg.source_dir / month_filename(dt.date.today())} -> "
                 f"{cfg.incoming} (poll {cfg.poll_seconds:g}s, settle {cfg.settle_seconds:g}s)")
    while not stop.is_set():
        watcher.step()
        stop.wait(cfg.poll_seconds)
    watcher.state = "stopped"
    watcher.write_heartbeat()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="look once and copy now if the workbook changed (skips the settle wait)")
    ap.add_argument("--env-file", help="read KEY=VALUE settings from this file first")
    args = ap.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    watcher = Watcher(Config.from_env())
    if args.once:
        state = watcher.step(settle_seconds=0)
        return 0 if state in ("copied", "up_to_date", "waiting_for_ingest") else 1
    return run_forever(watcher)


if __name__ == "__main__":
    sys.exit(main())
