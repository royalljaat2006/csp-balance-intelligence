"""
watch_transactions' --once pass must run all five schedules exactly once
— ingest_transactions, ingest_calling_sheet (near-live CALLING SHEET poll),
sync_daily_snapshots, poll_telegram, and run_autopilot. The loop itself
(interval sleeping, each schedule's own gate) isn't worth testing since
--once bypasses all five, but "does one pass actually trigger every job,
in order, even if one raises" is exactly the kind of wiring that's easy to
silently break in a refactor.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command

_ALL_SCHEDULES = [
    "ingest_transactions",
    "ingest_calling_sheet",
    "sync_daily_snapshots",
    "poll_telegram",
    "run_autopilot",
]


@pytest.mark.django_db
def test_once_runs_all_five_schedules():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        call_command("watch_transactions", once=True)

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == _ALL_SCHEDULES


@pytest.mark.django_db
def test_once_still_runs_the_rest_if_ingest_transactions_raises():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        mock_call.side_effect = [RuntimeError("bad file"), None, None, None, None]
        call_command("watch_transactions", once=True)

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == _ALL_SCHEDULES


@pytest.mark.django_db
def test_once_still_runs_the_rest_if_calling_sheet_raises():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        mock_call.side_effect = [None, RuntimeError("no credentials"), None, None, None]
        call_command("watch_transactions", once=True)

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == _ALL_SCHEDULES


@pytest.mark.django_db
def test_once_still_runs_the_rest_if_snapshot_sync_raises():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        mock_call.side_effect = [None, None, RuntimeError("no balances yet"), None, None]
        call_command("watch_transactions", once=True)

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == _ALL_SCHEDULES


@pytest.mark.django_db
def test_once_still_runs_autopilot_if_telegram_poll_raises():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        mock_call.side_effect = [None, None, None, RuntimeError("bad token"), None]
        call_command("watch_transactions", once=True)

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == _ALL_SCHEDULES


@pytest.mark.django_db
def test_jobs_flag_restricts_which_schedules_run():
    """--jobs is the horizontal-scaling knob (scalability foundation,
    2026-09-21): an operator running one instance per job type (see
    compose.yml) needs each instance to touch only its own job, never the
    other four."""
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        call_command("watch_transactions", once=True, jobs="ingest_transactions,poll_telegram")

    called_commands = [c.args[0] for c in mock_call.call_args_list]
    assert called_commands == ["ingest_transactions", "poll_telegram"]


@pytest.mark.django_db
def test_jobs_flag_rejects_unknown_job_name():
    with pytest.raises(Exception, match="unknown job"):
        call_command("watch_transactions", once=True, jobs="not_a_real_job")


@pytest.mark.django_db
def test_second_replica_skips_a_job_already_locked_by_the_first():
    """Wiring try_acquire_lock() into the scheduler (P0 fast-pass,
    2026-09-21): two "replicas" (simulated as two separate --once runs
    without releasing in between) must not both execute the same job."""
    from common.cache import try_acquire_lock

    assert try_acquire_lock("watch:ingest_transactions", ttl=600) is True
    # Simulates a second replica's tick arriving while the first is still
    # "running" (lock not yet released) — must be skipped, not duplicated.
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        call_command("watch_transactions", once=True, jobs="ingest_transactions")
    mock_call.assert_not_called()


@pytest.mark.django_db
def test_lock_is_released_after_the_job_so_the_next_tick_can_run():
    with patch("ingestion.management.commands.watch_transactions.call_command") as mock_call:
        call_command("watch_transactions", once=True, jobs="ingest_transactions")
        call_command("watch_transactions", once=True, jobs="ingest_transactions")
    assert mock_call.call_count == 2


@pytest.mark.django_db
def test_fast_schedule_does_not_wait_for_slow_ones():
    """Over several simulated ticks, the 60s calling-sheet/telegram
    schedules must keep firing while the 300s/86400s schedules stay quiet
    until their own interval elapses — proves the loop's sleep tick is
    min(...) rather than (say) the slowest interval, which would starve the
    near-live CALLING SHEET poll this feature exists for. time.time/
    time.sleep are faked so this runs instantly instead of waiting on a
    real clock."""
    calls = []
    # Starts at a large, arbitrary "current time" rather than 0 — matching
    # production, where the loop's last_run=0.0 sentinel ("never run yet")
    # is trivially far in the past compared to a real time.time() value, so
    # everything fires on the very first tick. Starting the fake clock at 0
    # would make that first-tick comparison (0 - 0 >= interval) false for
    # every schedule, which isn't how this behaves for real.
    fake_now = [1_000_000.0]
    start = fake_now[0]

    def fake_call_command(name, *a, **kw):
        calls.append(name)

    def fake_sleep(seconds):
        fake_now[0] += seconds
        if fake_now[0] >= start + 200:  # a handful of 60s ticks is enough to prove the point
            raise KeyboardInterrupt

    with (
        patch(
            "ingestion.management.commands.watch_transactions.call_command",
            side_effect=fake_call_command,
        ),
        patch(
            "ingestion.management.commands.watch_transactions.time.time",
            side_effect=lambda: fake_now[0],
        ),
        patch(
            "ingestion.management.commands.watch_transactions.time.sleep",
            side_effect=fake_sleep,
        ),
    ):
        with pytest.raises(KeyboardInterrupt):
            call_command(
                "watch_transactions", interval=300, calling_sheet_interval=60,
                snapshot_interval=300, telegram_interval=60, autopilot_interval=86400,
            )

    assert calls.count("ingest_calling_sheet") >= 3  # fired on (nearly) every tick
    assert calls.count("poll_telegram") >= 3  # same 60s cadence as calling-sheet
    assert calls.count("ingest_transactions") == 1  # 300s hasn't elapsed yet
    assert calls.count("sync_daily_snapshots") == 1  # 300s hasn't elapsed yet
    assert calls.count("run_autopilot") == 1  # 86400s hasn't elapsed yet
