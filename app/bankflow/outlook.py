"""
Month-end outlook for the SHADOW estimate -- read-only.

Reads only `DailyEstimate` (the shadow estimate store). Nothing here touches `DailyBalance`,
`MonthlySummary`, Rule #19 or any production figure, and nothing here is an SBI figure.

The agreed provisional calculation for one CSP and one calendar month:

  * every calendar day from the first stored estimate of the month counts once;
  * a day without its own stored estimate carries the previous day's estimate forward;
  * month-to-date average  = mean of those days up to and including `as_of`;
  * month-end projection   = the same mean with the LATEST estimate carried over every remaining
    calendar day of the month.

Three things are kept apart in the payload and must stay apart on screen:
  estimate     what the estimator stored for the days so far
  projection   that estimate carried to the end of the month
  SBI-reported the newest Avg Balance the Calling Sheet holds (a different, observed number)

The production month-end figures (csp.projection / MonthlySummary) use a different rule and are
shown only as a labelled, separate reference. They are not changed or reused here.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from csp.models import Csp, MonthlySummary
from django.db.models import Avg, Count
from django.utils import timezone

from bankflow import intelligence
from bankflow.models import DailyEstimate

ZERO = Decimal(0)
CENT = Decimal("0.01")


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(CENT)


def month_bounds(month: str | None, today: dt.date) -> tuple[str, dt.date, dt.date]:
    """(YYYY-MM, first day, last day); an invalid or missing month means the current one."""
    try:
        year, mon = (int(x) for x in (month or "").split("-"))
        first = dt.date(year, mon, 1)
    except ValueError:
        first = today.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return f"{first:%Y-%m}", first, last


def csp_outlook(
    by_day: dict[dt.date, Decimal], first: dt.date, last: dt.date, as_of: dt.date
) -> dict | None:
    """The calculation for ONE CSP. `by_day` maps a date to that day's stored estimate."""
    known = sorted(d for d in by_day if first <= d <= min(as_of, last))
    if not known:
        return None
    start = known[0]
    elapsed = (min(as_of, last) - start).days + 1
    total = ZERO
    carried = 0
    current = by_day[start]
    day = start
    for _ in range(elapsed):
        if day in by_day:
            current = by_day[day]
        else:
            carried += 1
        total += current
        day += dt.timedelta(days=1)
    remaining = (last - min(as_of, last)).days
    calendar_days = (last - first).days + 1
    mtd = total / elapsed
    projected = (total + current * remaining) / (elapsed + remaining)
    return {
        "mtd_average": _q(mtd),
        "projected_average": _q(projected),
        "latest_estimate": _q(current),
        "first_estimate_date": start,
        "days_counted": elapsed,
        "days_carried_forward": carried,
        "days_remaining": remaining,
        "calendar_days": calendar_days,
        "days_before_first_estimate": (start - first).days,
    }


def _per_csp(
    key: str, first: dt.date, last: dt.date, as_of: dt.date, csps: dict
) -> tuple[list, dict]:
    rows = (
        DailyEstimate.objects.filter(
            estimate_date__gte=first, estimate_date__lte=as_of, estimated_value__isnull=False,
            csp_id__in=list(csps),
        )
        .order_by("csp_id", "estimate_date")
        .values_list("csp_id", "estimate_date", "estimated_value", "status",
                     "current_account_count", "method")
    )
    values: dict[str, dict[dt.date, Decimal]] = defaultdict(dict)
    latest_meta: dict[str, tuple] = {}
    for code, d, v, status, n, method in rows:
        values[code][d] = v
        latest_meta[code] = (status, n, method)

    per_csp = []
    for code, by_day in values.items():
        o = csp_outlook(by_day, first, last, as_of)
        if o is None:
            continue
        status, n, method = latest_meta[code]
        per_csp.append({
            "csp_code": code, "name": csps[code].name, **o,
            "status": status, "method": method, "account_count": n,
            "provisional": (status != "VALIDATED" or o["days_before_first_estimate"] > 0
                            or o["days_carried_forward"] > 0),
        })
    return per_csp, latest_meta


def csp_outlook_map(month: str | None = None, today: dt.date | None = None) -> dict[str, dict]:
    """{csp_code: outlook row} for the month, used by the CSP list."""
    today = today or timezone.localdate()
    key, first, last = month_bounds(month, today)
    csps = {c.csp_code: c for c in Csp.objects.tracked()}
    per_csp, _ = _per_csp(key, first, last, min(today, last), csps)
    return {r["csp_code"]: r for r in per_csp}


def month_end_outlook(
    month: str | None = None, *, search: str = "", limit: int = 25, offset: int = 0,
    today: dt.date | None = None,
) -> dict:
    today = today or timezone.localdate()
    key, first, last = month_bounds(month, today)
    as_of = min(today, last)
    csps = {c.csp_code: c for c in Csp.objects.tracked()}
    per_csp, latest_meta = _per_csp(key, first, last, as_of, csps)

    def weighted(field: str):
        pairs = [(r[field], r["account_count"]) for r in per_csp
                 if r[field] is not None and r["account_count"]]
        tot = sum(n for _, n in pairs)
        return _q(sum(v * n for v, n in pairs) / tot) if tot else None

    needle = search.strip().lower()
    listed = [r for r in per_csp if not needle or needle in r["csp_code"].lower()
              or needle in (r["name"] or "").lower()]
    listed.sort(key=lambda r: r["csp_code"])

    sbi = intelligence.latest_observed(list(csps))
    sbi_weighted_pairs = [
        (o["avg"], values_n) for code, o in sbi.items()
        if (values_n := latest_meta.get(code, (None, None, None))[1])
    ]
    sbi_tot = sum(n for _, n in sbi_weighted_pairs)
    sbi_avg = _q(sum(v * n for v, n in sbi_weighted_pairs) / sbi_tot) if sbi_tot else None

    production = MonthlySummary.objects.filter(month=key).exclude(
        csp__status__in=["historical_only", "invalid_code"]
    ).aggregate(mtd=Avg("mtd_mab"), projected=Avg("projected_mab"), n=Count("id"))

    statuses: dict[str, int] = defaultdict(int)
    for r in per_csp:
        statuses[r["status"]] += 1
    warnings: list[str] = []
    if not per_csp:
        warnings.append("No estimate has been stored for this month yet, so there is no outlook.")
    else:
        if any(r["days_before_first_estimate"] > 0 for r in per_csp):
            warnings.append("Some CSPs have no estimate for the first days of the month; only "
                            "days from each CSP's first estimate are averaged.")
        if any(r["days_carried_forward"] for r in per_csp):
            warnings.append("Days without an estimate carry the previous estimate forward.")
        if statuses.get("VALIDATED", 0) != len(per_csp):
            warnings.append("The estimator has not been validated against SBI refreshes, so "
                            "every figure here is provisional and informational.")
    return {
        "month": key, "month_label": first.strftime("%B %Y"), "as_of": as_of,
        "first_day": first, "last_day": last,
        "days_elapsed": (as_of - first).days + 1, "days_remaining": (last - as_of).days,
        "calendar_days": (last - first).days + 1,
        "is_current_month": first <= today <= last,
        "csps_with_outlook": len(per_csp), "csps_tracked": len(csps),
        "by_status": dict(statuses),
        "network": {
            "mtd_average": weighted("mtd_average"),
            "projected_average": weighted("projected_average"),
            "latest_estimate": weighted("latest_estimate"),
            "weighting": "per-account averages weighted by each CSP's account count",
        },
        "sbi_reported": {"latest_average": sbi_avg, "csps": len(sbi_weighted_pairs)},
        "production_reference": {
            "mtd_average": _q(production["mtd"]) if production["mtd"] is not None else None,
            "projected_average": (_q(production["projected"])
                                  if production["projected"] is not None else None),
            "csps": production["n"],
            "note": "Production figures (Calling Sheet based, Rule #19 inputs). A different "
                    "calculation, shown for reference only.",
        },
        "rows": listed[offset: offset + limit], "total_rows": len(listed),
        "warnings": warnings,
        "assumptions": [
            "Each calendar day counts once; a day without a stored estimate carries the previous "
            "estimate forward.",
            "The projection carries the latest estimate over every remaining day of the month.",
            "Estimates are per-account averages. They are not SBI figures and are not used for "
            "Rule #19 or any incentive.",
        ],
    }
