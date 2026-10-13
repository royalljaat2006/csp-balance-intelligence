"""
The evaluation TARGET: what the Calling Sheet `Avg Balance` history actually
looks like, and where its independent refresh events are.

Nothing here assumes a refresh cadence. A "refresh event" is an observation
date on which a large share of CSPs changed value versus the previous
observation date; the thresholds are parameters, and the detected cadence is
reported as evidence, not hard-wired. (Local snapshot, 2026-09: 8 consecutive
dates identical, then one date where ~96% of CSPs changed -- i.e. a stepwise
series, polled far more often than it moves.)

`balance_date` is the date of the last write that day (DailyBalance keeps one
row per CSP per day); the sheet carries no measurement-date column, so the
effective measurement date is NOT known and is reported as an unresolved
assumption.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass

from csp.models import DailyBalance

DEFAULT_MIN_CHANGE_SHARE = 0.30
DEFAULT_MIN_COMPARED = 30


@dataclass(frozen=True)
class RefreshEvent:
    date: dt.date
    previous_date: dt.date
    compared: int
    changed: int
    # CSPs whose account_count did NOT change: a value change there is not just a
    # denominator effect, so these show whether the event is a genuine re-measurement.
    compared_acct_unchanged: int = 0
    changed_acct_unchanged: int = 0
    gap_days: int = 1  # days between the two observations; the true refresh lies inside the gap

    @property
    def share(self) -> float:
        return self.changed / self.compared if self.compared else 0.0


Observations = dict[dt.date, dict[str, tuple]]  # date -> csp -> (avg, account_count)


def load_observations() -> Observations:
    obs: Observations = defaultdict(dict)
    for csp_id, d, avg, acct in DailyBalance.objects.values_list(
        "csp_id", "balance_date", "daily_avg_balance", "account_count"
    ).iterator():
        obs[d][csp_id] = (avg, acct)
    return dict(obs)


def detect_refresh_events(
    obs: Observations,
    *,
    min_change_share: float = DEFAULT_MIN_CHANGE_SHARE,
    min_compared: int = DEFAULT_MIN_COMPARED,
    forced_dates: list[dt.date] | None = None,
) -> list[RefreshEvent]:
    """Consecutive-observation comparison. `forced_dates` lets an operator
    declare the real refresh dates (e.g. from sheet-owner confirmation or
    archive files) instead of relying on detection."""
    dates = sorted(obs)
    events: list[RefreshEvent] = []
    for prev, cur in zip(dates, dates[1:], strict=False):
        ev = _compare(obs, prev, cur)
        if forced_dates is not None:
            if cur in forced_dates:
                events.append(ev)
        elif ev.compared >= min_compared and ev.share >= min_change_share:
            events.append(ev)
    return events


def _compare(obs: Observations, prev: dt.date, cur: dt.date) -> RefreshEvent:
    common = obs[prev].keys() & obs[cur].keys()
    changed = 0
    same_acct = 0
    same_acct_changed = 0
    for c in common:
        value_changed = obs[cur][c][0] != obs[prev][c][0]
        changed += value_changed
        if obs[cur][c][1] == obs[prev][c][1]:
            same_acct += 1
            same_acct_changed += value_changed
    return RefreshEvent(
        date=cur,
        previous_date=prev,
        compared=len(common),
        changed=changed,
        compared_acct_unchanged=same_acct,
        changed_acct_unchanged=same_acct_changed,
        gap_days=(cur - prev).days,
    )


def target_report(
    *,
    min_change_share: float = DEFAULT_MIN_CHANGE_SHARE,
    min_compared: int = DEFAULT_MIN_COMPARED,
) -> dict:
    obs = load_observations()
    dates = sorted(obs)
    events = detect_refresh_events(
        obs, min_change_share=min_change_share, min_compared=min_compared
    )
    sources = _source_counts()
    gaps = [(b.date - a.date).days for a, b in zip(events, events[1:], strict=False)]

    per_date = []
    for prev, cur in zip(dates, dates[1:], strict=False):
        common = obs[prev].keys() & obs[cur].keys()
        changed = sum(1 for c in common if obs[cur][c][0] != obs[prev][c][0])
        acct_changed = sum(1 for c in common if obs[cur][c][1] != obs[prev][c][1])
        per_date.append(
            {
                "date": cur,
                "previous": prev,
                "compared": len(common),
                "avg_changed": changed,
                "account_count_changed": acct_changed,
            }
        )

    unresolved = [
        "Effective measurement date of Avg Balance (sheet has no as-on-date column).",
        "Whether the value is point-in-time, weekly, monthly or month-to-date.",
        "True refresh cadence: observed polling is far more frequent than value changes.",
        "Whether archived/backfilled (source=history) rows are comparable with live rows.",
    ]
    return {
        "observation_dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "sources": sources,
        "refresh_events": [
            {
                "date": e.date,
                "previous_date": e.previous_date,
                "compared": e.compared,
                "changed": e.changed,
                "share": round(e.share, 4),
                "compared_acct_unchanged": e.compared_acct_unchanged,
                "changed_acct_unchanged": e.changed_acct_unchanged,
                "gap_days": e.gap_days,
            }
            for e in events
        ],
        "n_refresh_events": len(events),
        "event_gap_days": gaps,
        "median_event_gap_days": statistics.median(gaps) if gaps else None,
        "per_date_changes": per_date,
        "detection": {"min_change_share": min_change_share, "min_compared": min_compared},
        "metric_definition": (
            "Per-account average = sheet 'Total Balance' / 'Total Accounts' "
            "(PRD Q2). Account-count changes move it independently of balances."
        ),
        "unresolved_assumptions": unresolved,
    }


def _source_counts() -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for (src,) in DailyBalance.objects.values_list("source").iterator():
        out[src] += 1
    return dict(out)


def month_end_mab_availability() -> dict:
    """Is there a ground truth for month-end MAB? Only if daily observations
    exist for (almost) every day AND the series is a genuinely daily
    measurement. A sparse/stepwise sheet series is neither, and must not be
    relabelled as true daily balances."""
    obs = load_observations()
    by_month: dict[str, list[dt.date]] = defaultdict(list)
    for d in sorted(obs):
        by_month[d.strftime("%Y-%m")].append(d)
    months: list[dict[str, float | int | str]] = []
    for month, ds in sorted(by_month.items()):
        y, m = map(int, month.split("-"))
        nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
        days_in_month = (nxt - dt.date(y, m, 1)).days
        distinct_values = len({tuple(sorted((c, v[0]) for c, v in obs[d].items())) for d in ds})
        months.append(
            {
                "month": month,
                "days_observed": len(ds),
                "days_in_month": days_in_month,
                "coverage": round(len(ds) / days_in_month, 3),
                "distinct_snapshots": distinct_values,
            }
        )
    complete = [
        m
        for m in months
        if float(m["coverage"]) >= 0.9
        and float(m["distinct_snapshots"]) >= 0.5 * float(m["days_observed"])
    ]
    return {
        "months": months,
        "months_with_usable_ground_truth": [m["month"] for m in complete],
        "available": bool(complete),
        "reason": (
            "No month has near-daily observations that actually vary day to day."
            if not complete
            else "Some months have near-daily varying observations."
        ),
        "needed": (
            "Daily closing/average balances per CSP for whole months (or SBI's own "
            "month-end figures -- explicitly out of scope for this phase)."
        ),
    }
