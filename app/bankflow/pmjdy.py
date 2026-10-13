"""
Snapshots of a CSP's TOTAL account count (AccountCountObservation): insert-only history, the
newest row is the current count, older rows exist for growth history. Nothing in this repo feeds
it yet -- total accounts currently come from csp.DailyBalance.account_count (the Calling Sheet's
"Total Accounts" column) -- but the dashboard prefers these snapshots when they exist.

New PMJDY accounts are a different measure (accounts OPENED per month) and live in
bankflow.pmjdy_monthly / PmjdyMonthly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from csp.models import Csp
from django.db import transaction
from django.utils import timezone

from bankflow.models import AccountCountObservation

SOURCE = "pmjdy"


def capture_counts(counts: dict[str, int], observed_at: dt.datetime | None = None) -> dict:
    """Insert-only. Idempotent: re-running with the same counts on the same IST day adds
    nothing. Counts for CSP codes we do not already track are reported, never created."""
    observed_at = observed_at or timezone.now()
    snap_date = timezone.localtime(observed_at).date()
    known = set(Csp.objects.filter(csp_code__in=counts).values_list("csp_code", flat=True))
    latest = latest_counts(known)
    have_today = set(
        AccountCountObservation.objects.filter(
            source=SOURCE, snapshot_date=snap_date, csp_id__in=known
        ).values_list("csp_id", flat=True)
    )
    new, changed, decreased = [], 0, 0
    for code in known:
        n = counts[code]
        prev = latest.get(code)
        is_change = prev is None or prev[0] != n
        if is_change or code not in have_today:
            new.append(
                AccountCountObservation(
                    csp_id=code, source=SOURCE, account_count=n,
                    observed_at=observed_at, snapshot_date=snap_date,
                )
            )
            if prev is not None and is_change:
                changed += 1
                decreased += n < prev[0]
    with transaction.atomic():
        AccountCountObservation.objects.bulk_create(new, ignore_conflicts=True, batch_size=1000)
    return {
        "inserted": len(new), "changed": changed, "decreased": decreased,
        "unchanged_not_inserted": len(known) - len(new),
        "unknown_codes": sorted(set(counts) - known)[:50],
        "n_unknown_codes": len(set(counts) - known),
        "snapshot_date": snap_date,
    }


def latest_counts(csp_ids=None) -> dict[str, tuple[int, dt.datetime]]:
    """csp_code -> (latest count, observed_at). One query (DISTINCT ON)."""
    qs = AccountCountObservation.objects.filter(source=SOURCE)
    if csp_ids is not None:
        qs = qs.filter(csp_id__in=list(csp_ids))
    rows = qs.order_by("csp_id", "-observed_at").distinct("csp_id")
    return {r.csp_id: (r.account_count, r.observed_at) for r in rows}


def counts_as_of(date: dt.date, csp_ids=None) -> dict[str, tuple[int, dt.datetime]]:
    """Newest observation on or before the END of `date` (IST) for each CSP."""
    qs = AccountCountObservation.objects.filter(source=SOURCE, snapshot_date__lte=date)
    if csp_ids is not None:
        qs = qs.filter(csp_id__in=list(csp_ids))
    rows = qs.order_by("csp_id", "-observed_at").distinct("csp_id")
    return {r.csp_id: (r.account_count, r.observed_at) for r in rows}


def daily_network_counts(date_from: dt.date, date_to: dt.date) -> list[dict]:
    """Network total of each CSP's latest count as of every snapshot date in range,
    carried forward within the range. Dates before a CSP's first observation contribute
    nothing for that CSP (never zero-filled)."""
    obs = (
        AccountCountObservation.objects.filter(source=SOURCE, snapshot_date__lte=date_to)
        .order_by("csp_id", "observed_at")
        .values_list("csp_id", "snapshot_date", "account_count")
    )
    by_csp: dict[str, list[tuple[dt.date, int]]] = defaultdict(list)
    for c, d, n in obs.iterator():
        series = by_csp[c]
        if series and series[-1][0] == d:
            series[-1] = (d, n)  # last observation of the day wins
        else:
            series.append((d, n))
    days = [date_from + dt.timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    totals = {d: 0 for d in days}
    reporting = {d: 0 for d in days}
    for series in by_csp.values():
        i, cur = 0, None
        for d in days:
            while i < len(series) and series[i][0] <= d:
                cur = series[i][1]
                i += 1
            if cur is not None:
                totals[d] += cur
                reporting[d] += 1
    out, prev_total = [], None
    for d in days:
        t = totals[d] if reporting[d] else None
        out.append(
            {
                "date": d, "total_accounts": t, "csps_reporting": reporting[d],
                "growth": (t - prev_total) if t is not None and prev_total is not None else None,
            }
        )
        if t is not None:
            prev_total = t
    return out
