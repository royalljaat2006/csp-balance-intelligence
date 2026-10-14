"""
Data progress: what each automated step has done today and yesterday, and when.

One function, `pipeline_progress`, feeds the Data Progress page and
GET /api/v1/bankflow/progress. It only READS what the pipeline already writes (IngestLog,
ImportRun, StatementFile, BaselineSnapshot, PmjdyMonthly, FlowAggregate, HypothesisRun,
DailyEstimate): there is no second job-tracking system, so this can never disagree with the data.

"Today" is the present IST day and "yesterday" the day before (the project's TIME_ZONE). A step
that has not run yet today is reported as waiting / late, never as done.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

from csp.models import IngestLog
from django.conf import settings
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from .models import (
    BaselineSnapshot,
    DailyEstimate,
    FlowAggregate,
    HypothesisRun,
    ImportRun,
    PmjdyMonthly,
    StatementFile,
)

LATE_GRACE_SECONDS = 300  # the worker wakes every 5 minutes; allow one extra tick before "late"
RECENT_FILES = 12
_EST_WORDS = {
    "VALIDATED": "validated",
    "PROVISIONAL": "provisional",
    "INSUFFICIENT_EVIDENCE": "not enough history yet",
    "UNAVAILABLE": "unavailable",
}


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of an IST calendar day as aware datetimes."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=timezone.get_current_timezone())
    return start, start + dt.timedelta(days=1)


def _snapshot_due(day: dt.date) -> dt.datetime:
    hh, mm = map(int, settings.BASELINE_SNAPSHOT_AT.split(":"))
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=timezone.get_current_timezone())


def _12h(ts: dt.datetime) -> str:
    return f"{timezone.localtime(ts):%I:%M %p}".lstrip("0")


def _clock(ts: dt.datetime | None) -> str | None:
    return None if ts is None else timezone.localtime(ts).strftime("%H:%M")


def _day(count: int, first, last, *, state: str, summary: str, **extra) -> dict:
    return {"count": count, "first_at": first, "last_at": last, "state": state,
            "summary": summary, **extra}


def _health(last_at, now, every_seconds: int) -> str:
    if last_at is None:
        return "never"
    age = (now - last_at).total_seconds()
    return "on_time" if age <= every_seconds * 2 + LATE_GRACE_SECONDS else "late"


def _log_day(source: str, day: dt.date) -> dict:
    start, end = day_bounds(day)
    qs = IngestLog.objects.filter(source=source, started_at__gte=start, started_at__lt=end)
    # one aggregate query for every figure (this used to be four queries per source per day)
    a = qs.aggregate(
        n=Count("id"), first=Min("started_at"), last=Max("started_at"),
        failed=Count("id", filter=Q(status=IngestLog.Status.FAILED)),
        last_ok=Max("finished_at", filter=Q(status__in=[IngestLog.Status.SUCCESS,
                                                        IngestLog.Status.PARTIAL])),
    )
    error = ""
    if a["n"]:
        error = qs.order_by("-started_at").values_list("error_summary", flat=True).first() or ""
    return {"n": a["n"], "first": a["first"], "last": a["last"], "failed": a["failed"],
            "last_ok": a["last_ok"], "last_error": error}


def _runs_state(day_is_today: bool, n: int, failed: int) -> str:
    if n:
        return "failed" if failed == n else "done"
    return "waiting" if day_is_today else "missing"


def _step_calling_sheet(day: dt.date, today: dt.date) -> dict:
    d = _log_day("calling_sheet", day)
    state = _runs_state(day == today, d["n"], d["failed"])
    summary = (f"Read {d['n']} times, first at {_clock(d['first'])}, last at {_clock(d['last'])}"
               if d["n"] else "Not read yet" if day == today else "No reads recorded")
    if d["failed"]:
        summary += f" ({d['failed']} failed)"
    return _day(d["n"], d["first"], d["last"], state=state, summary=summary, error=d["last_error"])


def _step_snapshot(day: dt.date, today: dt.date, now: dt.datetime) -> dict:
    qs = BaselineSnapshot.objects.filter(snapshot_date=day)
    a = qs.aggregate(n=Count("id"), first=Min("captured_at"), last=Max("captured_at"))
    due = _snapshot_due(day)
    log = _log_day("baseline_snapshot", day)
    if a["n"]:
        late = (a["first"] - due) > dt.timedelta(hours=2)
        summary = f"Captured at {_clock(a['first'])} for {a['n']} CSPs"
        if late:
            summary += " (later than planned: the worker was down at 6:00 AM)"
        return _day(a["n"], a["first"], a["last"], state="done", summary=summary, late=late,
                    due=due, error=log["last_error"])
    if day == today and now < due:
        return _day(0, None, None, state="waiting", due=due, late=False, error="",
                    summary=f"Waiting: taken once a day at {_12h(due)}")
    overdue = " yet. It should have run by now" if day == today else ""
    return _day(0, None, None, state="late" if day == today else "missing", due=due, late=False,
                summary="Not captured" + overdue, error=log["last_error"])


def _step_new_accounts(day: dt.date, today: dt.date) -> dict:
    d = _log_day("pmjdy", day)
    rows = PmjdyMonthly.objects.filter(snapshot_date=day).aggregate(
        n=Count("id"), last=Max("observed_at"))
    state = _runs_state(day == today, d["n"], d["failed"])
    summary = (f"Checked {d['n']} times, last at {_clock(d['last'])}; "
               f"{rows['n']} new readings stored" if d["n"]
               else "Not checked yet" if day == today else "No checks recorded")
    if d["failed"]:
        summary += f" ({d['failed']} failed)"
    return _day(d["n"], d["first"], d["last"], state=state, summary=summary,
                readings=rows["n"], error=d["last_error"])


def _step_statements(day: dt.date, today: dt.date) -> dict:
    start, end = day_bounds(day)
    files = StatementFile.objects.filter(imported_at__gte=start, imported_at__lt=end)
    a = files.aggregate(n=Count("id"), first=Min("imported_at"), last=Max("imported_at"),
                        rows=Sum("row_count"))
    runs = ImportRun.objects.filter(started_at__gte=start, started_at__lt=end).aggregate(
        n=Count("id"), last=Max("started_at"), failed=Sum("files_failed"))
    if a["n"]:
        summary = (f"{a['n']} statement file(s) ingested between {_clock(a['first'])} and "
                   f"{_clock(a['last'])} ({a['rows'] or 0:,} transactions)")
        state = "done"
    elif runs["n"]:
        summary = (f"Folder checked {runs['n']} times, last at {_clock(runs['last'])}; "
                   "no new statement file arrived")
        state = "done"
    else:
        summary = "Folder not checked yet" if day == today else "No checks recorded"
        state = "waiting" if day == today else "missing"
    return _day(a["n"], a["first"], a["last"], state=state, summary=summary,
                checks=runs["n"], last_check=runs["last"], rows=a["rows"] or 0)


def _step_transactions(day: dt.date, today: dt.date) -> dict:
    """The monthly transaction workbook ("Transaction Oct'26.xlsx"): the host watchdog copies it
    into incoming/ when it changes and `ingest_transactions` reads it. It arrives when the other
    team saves it, so this is information, not a schedule that can be late."""
    d = _log_day("transactions", day)
    if d["n"]:
        summary = (f"{d['n']} workbook ingest(s), first at {_clock(d['first'])}, "
                   f"last at {_clock(d['last'])}")
        if d["failed"]:
            summary += f" ({d['failed']} failed)"
        state = "failed" if d["failed"] == d["n"] else "done"
    else:
        summary = "No new workbook ingested yet" if day == today else "No workbook ingested"
        state = "waiting" if day == today else "missing"
    return _day(d["n"], d["first"], d["last"], state=state, summary=summary,
                error=d["last_error"])


def _step_flows(day: dt.date, today: dt.date) -> dict:
    start, end = day_bounds(day)
    a = FlowAggregate.objects.filter(updated_at__gte=start, updated_at__lt=end).aggregate(
        n=Count("flow_date", distinct=True), first=Min("updated_at"), last=Max("updated_at"))
    if a["n"]:
        return _day(a["n"], a["first"], a["last"], state="done",
                    summary=f"Money movement recalculated for {a['n']} statement day(s), "
                            f"last at {_clock(a['last'])}")
    return _day(0, None, None, state="waiting" if day == today else "missing",
                summary="Nothing to recalculate" if day == today else "No recalculation recorded")


def _step_verdict(day: dt.date, today: dt.date) -> dict:
    start, end = day_bounds(day)
    qs = HypothesisRun.objects.filter(created_at__gte=start, created_at__lt=end)
    a = qs.aggregate(n=Count("id"), first=Min("created_at"), last=Max("created_at"))
    latest = qs.order_by("-created_at").first()
    if a["n"] and latest is not None:
        return _day(a["n"], a["first"], a["last"], state="done",
                    summary=f"Checked at {_clock(a['last'])}: "
                            f"{_EST_WORDS.get(latest.status, latest.status.lower())}",
                    verdict=latest.status)
    return _day(0, None, None, state="waiting" if day == today else "missing",
                summary="Not checked yet today" if day == today else "No check recorded",
                verdict=None)


def _step_estimates(day: dt.date, today: dt.date) -> dict:
    qs = DailyEstimate.objects.filter(estimate_date=day)
    a = qs.aggregate(n=Count("id"), first=Min("calculated_at"), last=Max("calculated_at"))
    if a["n"]:
        by = Counter(qs.values_list("status", flat=True))
        words = ", ".join(f"{n} {_EST_WORDS.get(s, s.lower())}" for s, n in by.most_common())
        return _day(a["n"], a["first"], a["last"], state="done",
                    summary=f"{a['n']} CSP estimates, last calculated at {_clock(a['last'])} "
                            f"({words})", by_status=dict(by))
    return _day(0, None, None, state="waiting" if day == today else "missing",
                summary="Not calculated yet" if day == today else "No estimates for this day",
                by_status={})


def _latest(model_qs, field: str):
    return model_qs.aggregate(m=Max(field))["m"]


def _statement_files(limit: int = RECENT_FILES) -> list[dict]:
    out = []
    for f in StatementFile.objects.order_by("-imported_at")[:limit]:
        out.append({
            "filename": f.filename,
            "statement_day": f.last_row_date or f.statement_end,
            "produced_at": f.file_modified_at,   # when the bank file itself was written
            "ingested_at": f.imported_at,        # when we read it
            "rows": f.row_count, "status": f.status,
            "reason": f.status_reason,
        })
    return out


def _events(day: dt.date, steps_day: dict, files: list[dict]) -> list[dict]:
    """Chronological list of what happened on `day`: one entry per notable moment."""
    ev: list[dict] = []

    def add(ts, step, text):
        if ts is not None:
            ev.append({"at": ts, "step": step, "text": text})

    s = steps_day
    add(s["calling_sheet"]["first_at"], "calling_sheet", "First read of the Calling Sheet")
    add(s["calling_sheet"]["last_at"], "calling_sheet", "Latest read of the Calling Sheet")
    add(s["snapshot"]["first_at"], "snapshot",
        f"Morning snapshot captured for {s['snapshot']['count']} CSPs")
    add(s["new_accounts"]["first_at"], "new_accounts", "First check of new accounts")
    add(s["new_accounts"]["last_at"], "new_accounts", "Latest check of new accounts")
    start, end = day_bounds(day)
    for f in StatementFile.objects.filter(imported_at__gte=start, imported_at__lt=end):
        d = f.last_row_date or f.statement_end
        add(f.imported_at, "statements",
            f"Statement for {d:%d %b} ingested ({f.row_count:,} transactions)" if d
            else f"Statement ingested ({f.row_count:,} transactions)")
    if "transactions" in s:
        add(s["transactions"]["first_at"], "transactions", "Transaction sheet ingested")
        if s["transactions"]["count"] > 1:
            add(s["transactions"]["last_at"], "transactions", "Transaction sheet ingested again")
    add(s["flows"]["last_at"], "flows", "Money movement recalculated")
    add(s["verdict"]["last_at"], "verdict", "Estimator checked against the history")
    add(s["estimates"]["last_at"], "estimates", "Daily estimates calculated")
    return sorted(ev, key=lambda e: e["at"])


def pipeline_progress(now: dt.datetime | None = None) -> dict:
    now = now or timezone.now()
    today = timezone.localdate(now)
    yesterday = today - dt.timedelta(days=1)
    s = settings

    def both(fn, *extra):
        return {"today": fn(today, today, *extra), "yesterday": fn(yesterday, today, *extra)}

    has_transactions = IngestLog.objects.filter(source="transactions").exists()
    per_day = {
        "calling_sheet": both(_step_calling_sheet),
        "snapshot": {"today": _step_snapshot(today, today, now),
                     "yesterday": _step_snapshot(yesterday, today, now)},
        "new_accounts": both(_step_new_accounts),
        "statements": both(_step_statements),
        **({"transactions": both(_step_transactions)} if has_transactions else {}),
        "flows": both(_step_flows),
        "verdict": both(_step_verdict),
        "estimates": both(_step_estimates),
    }

    last_sheet = _latest(IngestLog.objects.filter(source="calling_sheet"), "started_at")
    last_pm = _latest(IngestLog.objects.filter(source="pmjdy"), "started_at")
    last_import = _latest(ImportRun.objects.all(), "started_at")
    last_flow = _latest(FlowAggregate.objects.all(), "updated_at")
    last_verdict = _latest(HypothesisRun.objects.all(), "created_at")
    last_est = _latest(DailyEstimate.objects.all(), "calculated_at")
    last_snap = _latest(BaselineSnapshot.objects.all(), "captured_at")
    snap_today = per_day["snapshot"]["today"]

    def nxt(last, every):
        return None if last is None else last + dt.timedelta(seconds=every)

    snap_health = ("on_time" if snap_today["state"] == "done"
                   else "waiting" if snap_today["state"] == "waiting"
                   else "never" if last_snap is None else "late")
    snap_next = (_snapshot_due(today + dt.timedelta(days=1)) if snap_today["state"] == "done"
                 else snap_today["due"])
    last_statement_day = StatementFile.objects.filter(
        status=StatementFile.Status.IMPORTED).aggregate(m=Max("last_row_date"))["m"]
    statement_note = None
    if last_statement_day is None:
        statement_note = "No bank statement has been ingested yet."
    elif last_statement_day < yesterday:
        statement_note = (f"The newest statement ingested is for {last_statement_day:%d %b %Y}. "
                          "Newer ones appear here once the bank file reaches the server.")

    steps = [
        {"key": "calling_sheet", "title": "Calling Sheet read",
         "what": "Reads each CSP's account count and SBI-reported average balance.",
         "schedule": "Every minute",
         "health": _health(last_sheet, now, 60), "last_at": last_sheet,
         "next_due": nxt(last_sheet, 60), "note": None},
        {"key": "snapshot", "title": "Morning snapshot",
         "what": "Saves every CSP's Calling Sheet value as the day's starting point.",
         "schedule": f"Once a day at {_12h(_snapshot_due(today))} IST",
         "health": snap_health, "last_at": last_snap, "next_due": snap_next, "note": None},
        {"key": "new_accounts", "title": "New accounts",
         "what": "Reads how many new customer accounts each CSP has opened this month.",
         "schedule": "Every hour",
         "health": _health(last_pm, now, s.PMJDY_INTERVAL_SECONDS), "last_at": last_pm,
         "next_due": nxt(last_pm, s.PMJDY_INTERVAL_SECONDS), "note": None},
        {"key": "statements", "title": "Bank statements",
         "what": "Reads the daily bank statement file and stores every transaction.",
         "schedule": "Checked every hour",
         "health": _health(last_import, now, s.BANKFLOW_STATEMENT_INTERVAL_SECONDS),
         "last_at": last_import,
         "next_due": nxt(last_import, s.BANKFLOW_STATEMENT_INTERVAL_SECONDS),
         "note": statement_note},
        *([{"key": "transactions", "title": "Transaction sheet",
            "what": "The monthly transaction workbook that feeds daily activity and the trends.",
            "schedule": "When the workbook changes (a watchdog copies it in; read within 5 min)",
            "health": "info", "last_at": _latest(IngestLog.objects.filter(source="transactions"),
                                                 "started_at"),
            "next_due": None, "note": None}] if has_transactions else []),
        {"key": "flows", "title": "Money movement",
         "what": "Adds up each CSP's withdrawals and deposits from the statements.",
         "schedule": "Right after a new statement, and re-checked daily",
         "health": "info" if last_flow else "never", "last_at": last_flow, "next_due": None,
         "note": None},
        {"key": "verdict", "title": "Estimator check",
         "what": "Tests whether the estimate can be trusted, using the saved history.",
         "schedule": "Once a day",
         "health": _health(last_verdict, now, s.HYPOTHESIS_INTERVAL_SECONDS),
         "last_at": last_verdict, "next_due": nxt(last_verdict, s.HYPOTHESIS_INTERVAL_SECONDS),
         "note": None},
        {"key": "estimates", "title": "Daily estimates",
         "what": "Works out each CSP's estimated average balance for the day.",
         "schedule": "Every hour",
         "health": _health(last_est, now, s.ESTIMATE_INTERVAL_SECONDS), "last_at": last_est,
         "next_due": nxt(last_est, s.ESTIMATE_INTERVAL_SECONDS), "note": None},
    ]
    for st in steps:
        st.update(per_day[st["key"]])
        st["health_label"] = {
            "on_time": "On time", "late": "Late", "never": "Not run yet",
            "waiting": "Waiting", "info": "Up to date",
        }[st["health"]]

    problems = [st["title"] for st in steps if st["health"] in ("late", "never")]
    files = _statement_files()
    return {
        "now": now, "today": today, "yesterday": yesterday, "timezone": "IST",
        "overall": {
            "state": "attention" if problems else "ok",
            "headline": ("Everything is running on time." if not problems
                         else f"{len(problems)} step(s) need a look: " + ", ".join(problems) + "."),
            "late": problems,
        },
        "steps": steps,
        "statement_files": files,
        "latest_statement_day": last_statement_day,
        "timeline": {
            "today": _events(today, {k: v["today"] for k, v in per_day.items()}, files),
            "yesterday": _events(yesterday, {k: v["yesterday"] for k, v in per_day.items()}, files),
        },
    }
