"""
Series and tables for one CSP's detail page -- read-only, built from stored data.

Everything here is evidence next to the stored estimate: SBI-reported readings, the stored daily
estimates, eligible statement movement, account-count changes, statement gaps and the rows that
were excluded. Nothing is computed into a new estimate.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from csp.models import Csp, DailyBalance
from django.utils import timezone

from bankflow import account_hypothesis as ah
from bankflow import flows, intelligence, services
from bankflow.models import (
    DailyEstimate,
    FlowAggregate,
    StatementTransaction,
)

ZERO = Decimal(0)
COUNTED = ("withdrawal", "deposit")


def _f(v):
    return None if v is None else float(v)


def csp_series(csp: Csp, *, days: int = 60, today: dt.date | None = None) -> dict:
    today = today or timezone.localdate()
    start = today - dt.timedelta(days=days - 1)

    estimates = list(
        DailyEstimate.objects.filter(csp=csp, estimate_date__gte=start)
        .order_by("estimate_date")
        .values("estimate_date", "estimated_value", "baseline_value", "status", "method")
    )

    readings = list(
        DailyBalance.objects.filter(csp=csp)
        .exclude(source__in=intelligence.NOT_OBSERVATIONS)
        .order_by("balance_date")
        .values_list("balance_date", "daily_avg_balance", "account_count")
    )
    series_for_pairs = {csp.csp_code: [(d, v, n) for d, v, n in readings]}
    pairs = ah.pairs_from_series(series_for_pairs)

    # SBI refreshes: the first day each distinct value was seen, with the account count then
    refreshes: list[dict] = []
    for d, v, n in readings:
        if not refreshes or v != refreshes[-1]["value"]:
            refreshes.append({"received_on": d, "value": v, "account_count": n})
    for prev, cur in zip(refreshes, refreshes[1:], strict=False):
        cur["change"] = cur["value"] - prev["value"]

    count_changes: list[dict] = []
    last_n = None
    for d, _v, n in readings:
        if n is None:
            continue
        if last_n is not None and n != last_n:
            count_changes.append({"date": d, "from": last_n, "to": n, "change": n - last_n})
        last_n = n
    count_series = [(d, n) for d, _v, n in readings if n is not None and d >= start]

    # eligible movement per day (exact CSP matches; withdrawals and deposits only)
    complete = flows.proven_complete_dates()
    daily = []
    for r in FlowAggregate.objects.filter(
        csp=csp, resolution_mode=flows.MODE_EXACT, flow_date__gte=start, flow_date__lte=today
    ).order_by("flow_date"):
        counted, _other = intelligence._split_flow(r.by_category)
        daily.append({
            "date": r.flow_date, "deposits": counted["deposits"],
            "withdrawals": counted["withdrawals"],
            "net": counted["deposits"] - counted["withdrawals"],
            "complete": r.flow_date in complete and r.flow_date < today,
        })

    # rows linked to this CSP but not counted, by category (excluded by rule or unlabelled)
    excluded: dict[tuple[str, str], list] = defaultdict(lambda: [0, ZERO])
    for row in StatementTransaction.objects.filter(
        resolution__resolved_csp=csp, resolution__status="exact", txn_date__gte=start,
    ).exclude(category__in=COUNTED).values("category", "channel_normalized", "credit", "debit"):
        key = (row["category"], row["channel_normalized"])
        excluded[key][0] += 1
        excluded[key][1] += row["credit"] + row["debit"]
    excluded_rows = [
        {"category": c, "label": lab.removeprefix("unknown:"), "count": v[0], "amount": v[1]}
        for (c, lab), v in sorted(excluded.items(), key=lambda kv: -kv[1][0])
    ]

    # statement days in the window that are missing or not proven complete
    cov = services.day_coverage()
    gaps = []
    d = start
    while d <= today:
        e = cov.get(d)
        if e is None and d < today:
            gaps.append({"date": d, "state": "no statement"})
        elif e is not None and e["class"] != services.COMPLETE_DAY:
            gaps.append({"date": d, "state": "partial" if e["class"] == services.PARTIAL_DAY
                         else "unverified"})
        d += dt.timedelta(days=1)

    return {
        "range": {"from": start, "to": today, "days": days},
        "estimates": estimates,
        "observed": [(d, _f(v)) for d, v, _n in readings if d >= start],
        "refreshes": list(reversed(refreshes))[:12],
        "count_series": count_series, "count_changes": list(reversed(count_changes))[:12],
        "daily_movement": daily, "excluded": excluded_rows, "gaps": gaps,
        "pairs": len(pairs),
        "counted_in_pairs": sum(1 for p in pairs if p.count_changed),
    }
