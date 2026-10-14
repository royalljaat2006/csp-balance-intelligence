"""The transaction-workbook watchdog (scripts/watch_transaction_sheet.py): copies the other team's
monthly workbook only once it has settled, atomically, never touches the source, never overwrites
a copy the app has not consumed, and tells you it is alive."""

import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "watch_transaction_sheet.py"
spec = importlib.util.spec_from_file_location("watch_transaction_sheet", SCRIPT)
wts = importlib.util.module_from_spec(spec)
sys.modules["watch_transaction_sheet"] = wts  # dataclasses look the module up by name
spec.loader.exec_module(wts)

IST = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 10, 14, 11, 0, tzinfo=IST).timestamp()
NAME = "Transaction Oct'26.xlsx"


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def env(tmp_path):
    source = tmp_path / "other_team"
    data = tmp_path / "data"
    source.mkdir()
    data.mkdir()
    cfg = wts.Config(source_dir=source, data_dir=data, poll_seconds=30, settle_seconds=45,
                     resync_seconds=6 * 3600, retention_days=14)
    clock = Clock()
    logs: list[str] = []
    watcher = wts.Watcher(cfg, clock=clock, log=logs.append)
    return watcher, clock, source, data, logs


def write_source(source, content=b"v1", name=NAME):
    p = source / name
    p.write_bytes(content)
    return p


def settle(watcher, clock, seconds=60):
    clock.advance(seconds)
    return watcher.step()


# ---------------------------------------------------------------- naming
def test_the_current_months_workbook_is_named_the_way_the_other_team_names_it():
    assert wts.month_filename(dt.date(2026, 10, 14)) == "Transaction Oct'26.xlsx"
    assert wts.month_filename(dt.date(2027, 1, 1)) == "Transaction Jan'27.xlsx"
    assert wts.month_filename(dt.date(2026, 12, 31)) == "Transaction Dec'26.xlsx"


def test_the_month_rolls_over_on_indian_time_not_utc(env):
    watcher, clock, source, _data, _ = env
    # 00:10 on 1 Nov in India is still 31 Oct in UTC
    clock.now = dt.datetime(2026, 11, 1, 0, 10, tzinfo=IST).timestamp()
    assert watcher.source_path().name == "Transaction Nov'26.xlsx"
    clock.now = dt.datetime(2026, 10, 31, 23, 50, tzinfo=IST).timestamp()
    assert watcher.source_path().name == "Transaction Oct'26.xlsx"


# ---------------------------------------------------------------- copying
def test_nothing_is_copied_until_the_file_has_stopped_changing(env):
    watcher, clock, source, data, _ = env
    write_source(source)
    assert watcher.step() == "settling"                      # just seen: not yet
    assert not (data / "incoming" / "transaction_sync.xlsx").exists()
    clock.advance(30)
    assert watcher.step() == "settling"                      # 30 s < 45 s settle
    clock.advance(20)
    assert watcher.step() == "copied"                        # quiet for 50 s
    assert (data / "incoming" / "transaction_sync.xlsx").read_bytes() == b"v1"


def test_a_file_that_keeps_being_written_is_never_copied_half_way(env):
    watcher, clock, source, data, _ = env
    for i in range(6):
        write_source(source, b"x" * (100 + i))               # the other team is still saving
        assert watcher.step() == "settling"
        clock.advance(30)                                    # but we poll before 45 s of quiet
    assert not (data / "incoming").exists() or not list((data / "incoming").iterdir())
    assert settle(watcher, clock, 60) == "copied"
    assert (data / "incoming" / "transaction_sync.xlsx").stat().st_size == 105


def test_the_copy_is_atomic_and_leaves_no_temp_file(env):
    watcher, clock, source, data, _ = env
    write_source(source, b"A" * 5_000_000)
    watcher.step()
    assert settle(watcher, clock) == "copied"
    names = sorted(p.name for p in (data / "incoming").iterdir())
    assert names == ["transaction_sync.xlsx"]                # no .part left behind
    assert (data / "incoming" / "transaction_sync.xlsx").stat().st_size == 5_000_000


def test_an_unchanged_file_is_not_copied_again(env):
    watcher, clock, source, data, _ = env
    write_source(source)
    watcher.step()
    assert settle(watcher, clock) == "copied"
    (data / "incoming" / "transaction_sync.xlsx").unlink()   # the app ingested it
    assert settle(watcher, clock, 600) == "up_to_date"
    assert not (data / "incoming" / "transaction_sync.xlsx").exists()
    assert watcher.copies == 1


def test_a_changed_file_is_copied_again_once_the_app_has_taken_the_previous_copy(env):
    watcher, clock, source, data, _ = env
    dest = data / "incoming" / "transaction_sync.xlsx"
    write_source(source, b"v1")
    watcher.step()
    settle(watcher, clock)
    dest.unlink()                                            # ingested and filed away
    write_source(source, b"v2-longer")
    watcher.step()
    assert settle(watcher, clock) == "copied"
    assert dest.read_bytes() == b"v2-longer" and watcher.copies == 2


def test_a_copy_the_app_has_not_consumed_yet_is_never_overwritten(env):
    watcher, clock, source, data, logs = env
    dest = data / "incoming" / "transaction_sync.xlsx"
    write_source(source, b"v1")
    watcher.step()
    settle(watcher, clock)
    write_source(source, b"v2-newer")                        # changes while v1 is still waiting
    watcher.step()
    assert settle(watcher, clock) == "waiting_for_ingest"
    assert dest.read_bytes() == b"v1"                        # untouched
    assert settle(watcher, clock, 45 * 60) == "waiting_for_ingest"
    assert any("has not been ingested" in line for line in logs)   # and it says so
    dest.unlink()                                            # worker-ingestion took it
    assert settle(watcher, clock, 30) == "copied"
    assert dest.read_bytes() == b"v2-newer"                  # the NEWEST version, not a stale one


def test_a_copy_that_races_with_a_write_is_discarded_and_retried(env, monkeypatch):
    watcher, clock, source, data, _ = env
    src = write_source(source, b"v1")
    watcher.step()
    clock.advance(60)
    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *a, **kw):
        if self == src:
            calls["n"] += 1
            if calls["n"] == 2:                              # the re-check after reading
                src.write_bytes(b"v1-but-now-different-size")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert watcher.step() == "source_changed_during_copy"
    monkeypatch.undo()
    assert not list((data / "incoming").glob("*"))           # nothing half-copied is left
    watcher.step()
    assert settle(watcher, clock) == "copied"
    dest = data / "incoming" / "transaction_sync.xlsx"
    assert dest.read_bytes() == b"v1-but-now-different-size"


# ---------------------------------------------------------------- the source
def test_a_missing_workbook_is_waited_for_not_a_crash(env):
    watcher, clock, source, data, logs = env
    assert watcher.step() == "waiting_for_source"
    assert watcher.step() == "waiting_for_source"
    assert sum("does not exist yet" in line for line in logs) == 1   # logged once, not every poll
    write_source(source, b"here")
    watcher.step()
    assert settle(watcher, clock) == "copied"


def test_the_source_directory_is_never_written_to(env):
    watcher, clock, source, _data, _ = env
    src = write_source(source, b"precious")
    before = (src.stat().st_mtime_ns, src.stat().st_size, sorted(p.name for p in source.iterdir()))
    watcher.step()
    settle(watcher, clock)
    settle(watcher, clock, 3600)
    after = (src.stat().st_mtime_ns, src.stat().st_size, sorted(p.name for p in source.iterdir()))
    assert before == after and src.read_bytes() == b"precious"


def test_a_safety_resync_heals_a_lost_ingest(env):
    watcher, clock, source, data, _ = env
    dest = data / "incoming" / "transaction_sync.xlsx"
    write_source(source)
    watcher.step()
    settle(watcher, clock)
    dest.unlink()                                            # ingested -- or lost; we cannot tell
    assert settle(watcher, clock, 3600) == "up_to_date"
    assert settle(watcher, clock, 6 * 3600) == "copied"      # 6 h later: copy once more
    assert watcher.copies == 2


# ---------------------------------------------------------------- housekeeping
def test_only_our_own_old_copies_in_processed_are_pruned(env):
    watcher, clock, source, data, _ = env
    processed = data / "processed"
    processed.mkdir()
    old, fresh, other = (processed / "transaction_sync.20260801T000000.xlsx",
                         processed / "transaction_sync.20261013T000000.xlsx",
                         processed / "a_real_upload.20260801T000000.xlsx")
    for p in (old, fresh, other):
        p.write_bytes(b"x")
    month_ago, yesterday = clock() - 30 * 86400, clock() - 86400
    os.utime(old, (month_ago, month_ago))
    os.utime(other, (month_ago, month_ago))
    os.utime(fresh, (yesterday, yesterday))
    write_source(source)
    watcher.step()
    settle(watcher, clock)
    assert not old.exists() and fresh.exists() and other.exists()


def test_the_heartbeat_says_it_is_alive_and_what_it_last_did(env):
    watcher, clock, source, data, _ = env
    write_source(source, b"abc")
    watcher.step()
    settle(watcher, clock)
    beat = json.loads((data / "transaction_watchdog.json").read_text())
    assert beat["state"] == "copied" and beat["copies_total"] == 1
    assert beat["source_file"] == NAME and beat["last_copy_bytes"] == 3
    assert beat["pid"] == os.getpid() and beat["poll_seconds"] == 30
    assert beat["last_poll_at"].endswith("+05:30") and beat["last_copy_at"] is not None
    assert not list(data.glob("*.tmp"))                      # written atomically


def test_an_unexpected_error_is_reported_and_the_loop_survives(env, monkeypatch):
    watcher, clock, source, data, _ = env
    write_source(source)
    watcher.step()
    clock.advance(60)

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(watcher, "_copy", boom)
    assert watcher.step() == "error"
    beat = json.loads((data / "transaction_watchdog.json").read_text())
    assert beat["state"] == "error" and "PermissionError" in beat["last_error"]
    monkeypatch.undo()
    assert watcher.step() == "copied"                        # next poll simply tries again


# ---------------------------------------------------------------- configuration
def test_configuration_comes_from_the_environment_and_requires_the_data_dir(tmp_path):
    with pytest.raises(SystemExit):
        wts.Config.from_env({})
    cfg = wts.Config.from_env({
        "TRANSACTION_DATA_DIR_HOST": str(tmp_path), "TRANSACTION_WATCH_POLL_SECONDS": "10",
        "TRANSACTION_WATCH_SETTLE_SECONDS": "5", "TRANSACTION_WATCH_RESYNC_HOURS": "0",
        "TRANSACTION_SOURCE_DIR": "/elsewhere"})
    assert cfg.poll_seconds == 10 and cfg.settle_seconds == 5 and cfg.resync_seconds == 0
    assert cfg.source_dir == Path("/elsewhere") and cfg.incoming == tmp_path / "incoming"
    default = wts.Config.from_env({"TRANSACTION_DATA_DIR_HOST": str(tmp_path)})
    assert default.source_dir == Path("/home/shumit/sbikisok") and default.settle_seconds == 45


def test_once_copies_immediately_without_the_settle_wait(env, monkeypatch):
    watcher, _clock, source, data, _ = env
    this_month = wts.month_filename(dt.datetime.now(IST).date())
    write_source(source, b"now", name=this_month)
    monkeypatch.setenv("TRANSACTION_DATA_DIR_HOST", str(data))
    monkeypatch.setenv("TRANSACTION_SOURCE_DIR", str(source))
    assert wts.main(["--once"]) == 0
    assert (data / "incoming" / "transaction_sync.xlsx").read_bytes() == b"now"


def test_an_env_file_is_read_and_real_environment_variables_win(tmp_path):
    f = tmp_path / "watchdog.env"
    lines = ["# a comment", "", "TRANSACTION_DATA_DIR_HOST=/from/file",
             "TRANSACTION_SOURCE_DIR = '/quoted/dir'", "TRANSACTION_WATCH_POLL_SECONDS=5"]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {"TRANSACTION_WATCH_POLL_SECONDS": "99"}
    wts.load_env_file(f, env)
    assert env["TRANSACTION_DATA_DIR_HOST"] == "/from/file"
    assert env["TRANSACTION_SOURCE_DIR"] == "/quoted/dir"
    assert env["TRANSACTION_WATCH_POLL_SECONDS"] == "99"      # the real environment wins
    assert wts.Config.from_env(env).data_dir == Path("/from/file")
