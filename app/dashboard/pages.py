"""
The journey pages: Overview, Data readiness, CSP balances (list + explanation), Review, SBI
comparison and Month-end outlook.

Views here only gather what the domain layers already compute (csp.comparison, bankflow.*) and hand
it to templates. No formula lives here: estimates, variances, outlooks and gates come from
bankflow.estimates / explain / outlook / sbi_comparison / review / readiness.
"""

from __future__ import annotations

import datetime as dt
from functools import partial
from urllib.parse import urlencode

from bankflow import csp_view, explain, outlook, readiness, review, sbi_comparison
from bankflow import estimates as bankflow_estimates
from bankflow import intelligence as bankflow_intelligence
from bankflow import progress as bankflow_progress
from bankflow import services as bankflow_services
from common.cache import cache_aside
from csp import comparison, services
from csp.models import Csp
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone

from . import plain
from .charts import spec as chart_spec
from .views import _paginate

_RANGES = (7, 30, 90)
PLAIN = bankflow_intelligence.plain


def _ttl() -> int:
    return settings.DASHBOARD_CACHE_TTL


def _days(request, default: int = 30) -> int:
    try:
        d = int(request.GET.get("range", default))
    except ValueError:
        return default
    return d if d in _RANGES else default


def _qs(request, **over) -> str:
    q = {k: v for k, v in request.GET.items() if k != "page"}
    q.update({k: v for k, v in over.items() if v not in (None, "")})
    return urlencode(q)


# ============================================================================ Overview
@login_required
def home(request):
    today = timezone.localdate()
    days = 30
    frm, to = bankflow_intelligence.resolve_range(days)
    summary = cache_aside(f"ui:summary:{days}", ttl=_ttl(),
                          fn=lambda: bankflow_intelligence.dashboard_summary(frm, to))
    movement = cache_aside(f"ui:movement:{days}", ttl=_ttl(),
                           fn=lambda: bankflow_intelligence.network_movement(frm, to))
    exc = cache_aside(f"ui:exc:{days}", ttl=_ttl(),
                      fn=lambda: bankflow_intelligence.exceptions(frm, to, limit=200))
    ready = cache_aside(f"ui:readiness:{days}:{today}", ttl=_ttl(),
                        fn=lambda: readiness.data_readiness(days=days, today=today))
    rev = cache_aside(f"ui:review:{days}:{today}", ttl=_ttl(),
                      fn=lambda: review.review_queue(days, today=today))
    out = cache_aside(f"ui:outlook:{today}", ttl=_ttl(),
                      fn=lambda: outlook.month_end_outlook(limit=0, today=today))
    cmp_rows = cache_aside(
        f"ui:cmp:{today}", ttl=_ttl(),
        fn=lambda: comparison.bulk_compare_csps(today, comparison.resolve_comparison_date(today),
                                                mode="overall"))

    cats = {c["key"]: c for c in rev["categories"]}
    est = summary["estimates"]
    by = est["by_status"]
    reliable = by.get("VALIDATED", 0)
    unavailable = by.get("UNAVAILABLE", 0)
    provisional = sum(v for k, v in by.items() if k not in ("VALIDATED", "UNAVAILABLE"))
    bucket = comparison.MovementBucket
    big_up = sum(1 for c in cmp_rows if c.balance.movement == bucket.STRONG_GROWTH)
    big_down = sum(1 for c in cmp_rows if c.balance.movement == bucket.SHARP_DECLINE)
    obs = summary["observed_avg_balance"]
    fr = summary["freshness"]

    cards = [plain.friendly_exception(i, days) for i in exc["items"] if i["csp_code"]]
    flows_ = [p for p in movement["series"] if p["withdrawals"] is not None]
    charts = {
        "money": chart_spec(
            "Customer withdrawals and deposits per day", "inr", [
                {"name": "Deposited by customers", "color": "s1",
                 "points": [[str(p["date"]), PLAIN(p["deposits"])] for p in flows_]},
                {"name": "Withdrawn by customers", "color": "s2",
                 "points": [[str(p["date"]), PLAIN(p["withdrawals"])] for p in flows_]},
            ], kind="bars", empty="No bank statement covers this period yet."),
        "average": chart_spec(
            "Average balance per account", "inr", [
                {"name": "SBI-reported (production source)", "color": "s1", "step": True,
                 "points": [[str(p["date"]), p["observed_avg_balance"]] for p in movement["series"]
                            if p["observed_avg_balance"] is not None]},
                {"name": "Our estimate (shadow, informational)", "color": "s2", "dash": True,
                 "points": [[str(p["date"]), p["estimated_avg_balance"]] for p in movement["series"]
                            if p["estimated_avg_balance"] is not None]},
            ], empty="No readings in this period yet."),
    }
    return render(request, "dashboard/overview.html", {
        "today": today, "summary": summary, "ready": ready, "review": rev, "cats": cats,
        "outlook": out, "fr": fr, "obs": obs, "notes": plain.data_notes(summary),
        "new_accounts": summary["new_accounts"], "accounts": summary["accounts"],
        "metrics": {
            "tracked": summary["csps_tracked"], "baseline_unit": f"of {summary['csps_tracked']}",
            "with_baseline": obs["csps_with_observation"],
            "reliable": reliable, "provisional": provisional, "unavailable": unavailable,
            "big_up": big_up, "big_down": big_down, "compared": len(cmp_rows),
            "estimate_date": est["estimate_date"],
            "unattributed_share": summary["flows"]["unattributed_share"],
        },
        "attention_cards": cards[:6], "attention_total": len(cards),
        "open_categories": [c for c in rev["categories"] if c["status"] == "open"],
        "charts": charts,
    })


# ============================================================================ Data readiness
@login_required
def data_readiness(request):
    days = _days(request)
    today = timezone.localdate()
    staff = request.user.is_staff
    ready = cache_aside(f"ui:readiness:{days}:{today}:{int(staff)}", ttl=_ttl(),
                        fn=lambda: readiness.data_readiness(days=days, today=today,
                                                            is_staff=staff))
    progress = bankflow_progress.pipeline_progress()
    if not request.user.is_staff:  # technical error text is for staff only
        for step in progress["steps"]:
            for day in ("today", "yesterday"):
                step[day]["error"] = ""
    return render(request, "dashboard/data_readiness.html", {
        "ready": ready, "st": {s["key"]: s for s in ready["stages"]}, "p": progress,
        "days": days, "ranges": _RANGES,
    })


# ============================================================================ CSP balances
_STATUS_SORT = {"VALIDATED": 0, "PROVISIONAL": 1, "INSUFFICIENT_EVIDENCE": 2, "UNAVAILABLE": 3}
_STATUS_FILTERS = {
    "all": "All CSPs", "review": "Needs review", "provisional": "Provisional estimate",
    "reliable": "Reliable estimate", "unavailable": "No estimate",
}
_SORTS = {
    "name": "Name", "estimate": "Estimated average", "month_end": "Month-end projection",
    "movement": "Today vs yesterday", "review": "Review items",
}


def _csp_rows(today: dt.date) -> list[dict]:
    """One row per tracked CSP, merged from what the domain layers already stored."""
    frm, to = bankflow_intelligence.resolve_range(30)
    growth, _t, _m = bankflow_intelligence.csp_growth(frm, to, limit=100000, offset=0)
    by_growth = {r["csp_code"]: r for r in growth}
    cmp_ = {c.csp_code: c for c in comparison.bulk_compare_csps(
        today, comparison.resolve_comparison_date(today), mode="overall")}
    outlooks = outlook.csp_outlook_map(today=today)
    readings = bankflow_intelligence._latest_readings(None, with_count=True)
    rows = []
    for csp in Csp.objects.tracked().order_by("csp_code"):
        g = by_growth.get(csp.csp_code, {})
        c = cmp_.get(csp.csp_code)
        o = outlooks.get(csp.csp_code)
        rd = readings.get(csp.csp_code)
        status = g.get("estimate_status", "NOT_COMPUTED")
        flags = [f for f in g.get("flags", []) if f["severity"] != "low"]
        count_issue = (csp.account_count in (None, 0))
        age = (today - rd[2]).days if rd else None
        rows.append({
            "code": csp.csp_code, "name": csp.name, "accounts": csp.account_count,
            "accounts_stale": age is None or age > 3, "accounts_age": age,
            "count_issue": count_issue,
            "sbi_avg": g.get("observed_avg"), "sbi_date": g.get("observed_last_seen"),
            "est": g.get("estimated_value"), "status": status,
            "est_method": g.get("estimate_method"),
            "move_pct": c.balance.pct_change if c else None,
            "move_abs": c.balance.abs_change if c else None,
            "mtd": o["mtd_average"] if o else None,
            "projected": o["projected_average"] if o else None,
            "days_counted": o["days_counted"] if o else None,
            "flags": flags, "review_items": len(flags) + (1 if count_issue else 0),
        })
    return rows


_DAY_SORTS = {
    "accounts_change": "Biggest rise in customer accounts", "new_today": "Most new accounts today",
    "withdrawn_today": "Most withdrawn today", "deposited_today": "Most deposited today",
    "net_today": "Deposits minus withdrawals today",
    "net_change": "Biggest change in deposits minus withdrawals", "name": "Name (A to Z)",
}


def _csp_day_view(request, today: dt.date):
    """Every CSP, the present day against the day before (account counts, new accounts, eligible
    withdrawals and deposits, SBI-reported average and our estimate). Rows come from
    intelligence.csp_day_compare_rows, the same function the API serves."""
    q = request.GET.get("q", "").strip()[:64]
    sort = request.GET.get("sort", "accounts_change")
    sort = sort if sort in _DAY_SORTS else "accounts_change"
    desc = request.GET.get("order", "asc" if sort == "name" else "desc") == "desc"
    page, offset, page_size = _paginate(request, page_size=25)
    rows, total, meta = bankflow_intelligence.csp_day_compare_rows(
        today, search=q, sort=sort, descending=desc, limit=page_size, offset=offset)
    return render(request, "dashboard/csp_directory.html", {
        "view": "day", "rows": rows, "total": total, "meta": meta, "q": q, "sort": sort,
        "sorts": _DAY_SORTS, "order": "desc" if desc else "asc", "page": page,
        "total_pages": max(1, -(-total // page_size)), "today": today,
        "show": "all", "filters": _STATUS_FILTERS, "all_total": total, "counts": {},
    })


@login_required
def csp_directory(request):
    today = timezone.localdate()
    if request.GET.get("view") == "day":
        return _csp_day_view(request, today)
    rows = cache_aside(f"ui:csp_rows:{today}", ttl=_ttl(), fn=lambda: _csp_rows(today))
    q = request.GET.get("q", "").strip()[:64]
    show = request.GET.get("show", "all")
    show = show if show in _STATUS_FILTERS else "all"
    sort = request.GET.get("sort", "name")
    sort = sort if sort in _SORTS else "name"
    desc = request.GET.get("order", "asc") == "desc"

    shown = rows
    if q:
        n = q.lower()
        shown = [r for r in shown if n in r["code"].lower() or n in (r["name"] or "").lower()]
    if show == "review":
        shown = [r for r in shown if r["review_items"]]
    elif show == "provisional":
        shown = [r for r in shown if r["status"] in ("PROVISIONAL", "INSUFFICIENT_EVIDENCE")]
    elif show == "reliable":
        shown = [r for r in shown if r["status"] == "VALIDATED"]
    elif show == "unavailable":
        shown = [r for r in shown if r["status"] in ("UNAVAILABLE", "NOT_COMPUTED")]

    def key(r):
        if sort == "estimate":
            v = r["est"]
        elif sort == "month_end":
            v = r["projected"]
        elif sort == "movement":
            v = r["move_pct"]
        elif sort == "review":
            v = r["review_items"]
        else:
            return ((r["name"] or "").lower(), r["code"])
        return (v is None, v if v is not None else 0)  # missing values always sort last

    shown = sorted(shown, key=key, reverse=desc)
    if sort != "name":  # keep "no value" at the end in both directions
        has = [r for r in shown if key(r)[0] is False]
        none = [r for r in shown if key(r)[0] is True]
        shown = has + none
    page, offset, page_size = _paginate(request, page_size=25)
    total = len(shown)
    total_pages = max(1, -(-total // page_size))
    return render(request, "dashboard/csp_directory.html", {
        "view": "balances", "rows": shown[offset: offset + page_size], "total": total,
        "all_total": len(rows),
        "q": q, "show": show, "filters": _STATUS_FILTERS, "sort": sort, "sorts": _SORTS,
        "order": "desc" if desc else "asc", "page": page, "total_pages": total_pages,
        "qs_prev": _qs(request, page=page - 1), "qs_next": _qs(request, page=page + 1),
        "counts": {
            "review": sum(1 for r in rows if r["review_items"]),
            "reliable": sum(1 for r in rows if r["status"] == "VALIDATED"),
            "provisional": sum(1 for r in rows if r["status"] in
                               ("PROVISIONAL", "INSUFFICIENT_EVIDENCE")),
            "none": sum(1 for r in rows if r["status"] in ("UNAVAILABLE", "NOT_COMPUTED")),
        },
        "today": today,
    })


@login_required
def csp_explain(request, csp_code: str):
    data = explain.explain_estimate(csp_code)
    if data is None:
        raise Http404(f"No CSP with code {csp_code}")
    return render(request, "dashboard/csp_explain.html", {"x": data, "code": csp_code})


def csp_detail_extras(csp: Csp) -> dict:
    """Extra context for the CSP page: evidence tables and chart specs (read-only)."""
    s = csp_view.csp_series(csp)
    est_pts = [[str(e["estimate_date"]), PLAIN(e["estimated_value"])] for e in s["estimates"]
               if e["estimated_value"] is not None]
    obs_pts = [[str(d), v] for d, v in s["observed"]]
    flow = s["daily_movement"]
    charts = {
        "average": chart_spec("Average balance per account", "inr", [
            {"name": "SBI-reported (production source)", "color": "s1", "step": True,
             "points": obs_pts},
            {"name": "Our estimate (shadow, informational)", "color": "s2", "dash": True,
             "points": est_pts},
        ], empty="No readings in the last 60 days."),
        "movement": chart_spec("Eligible deposits and withdrawals per day", "inr", [
            {"name": "Deposited by customers", "color": "s1",
             "points": [[str(r["date"]), PLAIN(r["deposits"])] for r in flow]},
            {"name": "Withdrawn by customers", "color": "s2",
             "points": [[str(r["date"]), PLAIN(r["withdrawals"])] for r in flow]},
            {"name": "Net (deposits minus withdrawals)", "color": "s3",
             "points": [[str(r["date"]), PLAIN(r["net"])] for r in flow]},
        ], kind="bars", empty="No eligible statement movement for this CSP in the last 60 days."),
        "accounts": chart_spec("Customer accounts", "count", [
            {"name": "Accounts", "color": "s1", "step": True,
             "points": [[str(d), n] for d, n in s["count_series"]]},
        ], empty="No account counts in the last 60 days."),
    }
    return {"series": s, "charts": charts}


# ============================================================================ Review
@login_required
def needs_attention(request):
    days = _days(request)
    today = timezone.localdate()
    is_staff = request.user.is_staff
    rev = cache_aside(f"ui:review:{days}:{today}:{int(is_staff)}", ttl=_ttl(),
                      fn=lambda: review.review_queue(days, is_staff=is_staff, today=today))
    frm, to = rev["range"]["from"], rev["range"]["to"]
    exc = cache_aside(f"ui:exc-full:{days}", ttl=_ttl(),
                      fn=lambda: bankflow_intelligence.exceptions(frm, to, limit=500))
    all_cards = [plain.friendly_exception(i, days) for i in exc["items"] if i["csp_code"]]
    network = [plain.friendly_exception(i, days) for i in exc["items"] if not i["csp_code"]]
    titles = {c["type"]: c["title"] for c in all_cards}
    kind = request.GET.get("type", "")
    cards = [c for c in all_cards if c["type"] == kind] if kind in titles else all_cards
    open_cat = request.GET.get("c", "")
    page, offset, page_size = _paginate(request, page_size=15)
    return render(request, "dashboard/review.html", {
        "review": rev, "days": days, "ranges": _RANGES,
        "cards": cards[offset: offset + page_size], "total": len(cards),
        "page": page, "total_pages": max(1, -(-len(cards) // page_size)),
        "qs_prev": _qs(request, page=page - 1), "qs_next": _qs(request, page=page + 1),
        "kind": kind if kind in titles else "", "type_choices": sorted(titles.items(),
                                                                       key=lambda kv: kv[1]),
        "network": network, "open_cat": open_cat,
        "high": sum(1 for c in all_cards if c["severity"] == "high"),
        "medium": sum(1 for c in all_cards if c["severity"] == "medium"),
    })


# ============================================================================ SBI comparison
@login_required
def bank_flow(request):
    """Estimate vs SBI-reported value, and whether the agreed validation gate is met. Staff only,
    strictly read-only; an observed result is kept apart from insufficient evidence."""
    if not request.user.is_staff:
        return HttpResponseForbidden("SBI comparison is restricted to staff accounts.")
    only_changed = request.GET.get("scope", "all") == "changed"
    q = request.GET.get("q", "").strip()[:32]
    page, offset, page_size = _paginate(request, page_size=25)
    data = sbi_comparison.sbi_comparison(
        only_count_changed=only_changed, search=q, limit=page_size, offset=offset)
    evals = bankflow_services.evaluation_overview()
    return render(request, "dashboard/sbi_comparison.html", {
        "c": data, "q": q, "scope": "changed" if only_changed else "all",
        "page": page, "total": data["total_rows"],
        "total_pages": max(1, -(-data["total_rows"] // page_size)),
        "qs_prev": _qs(request, page=page - 1), "qs_next": _qs(request, page=page + 1),
        "estimate_overview": bankflow_estimates.estimate_overview(),
        "evaluations": evals,
    })


# ============================================================================ Month-end outlook
@login_required
def month_end(request):
    month = request.GET.get("month", "")
    q = request.GET.get("q", "").strip()[:32]
    page, offset, page_size = _paginate(request, page_size=25)
    data = outlook.month_end_outlook(month or None, search=q, limit=page_size, offset=offset)
    months = sorted({e for e in _outlook_months()}, reverse=True)
    return render(request, "dashboard/month_end.html", {
        "o": data, "q": q, "months": months, "page": page, "total": data["total_rows"],
        "total_pages": max(1, -(-data["total_rows"] // page_size)),
        "qs_prev": _qs(request, page=page - 1), "qs_next": _qs(request, page=page + 1),
        "month_key": data["month"],
    })


def _outlook_months() -> list[str]:
    from bankflow.models import DailyEstimate
    from django.db.models.functions import TruncMonth

    ms = [f"{m:%Y-%m}" for m in DailyEstimate.objects.annotate(m=TruncMonth("estimate_date"))
          .values_list("m", flat=True).distinct()]
    return ms or [services.current_month()]


def legacy_redirect(name: str):
    """A permanent-enough redirect for a page that moved, keeping the query string."""
    def view(request):
        target = redirect(name)
        qs = request.META.get("QUERY_STRING", "")
        return redirect(f"{target.url}?{qs}" if qs else target.url)
    return view


def warm() -> None:
    """Pre-compute the journey pages' shared figures under the same cache keys the views use, so
    the first visitor after a data change does not wait for them (see dashboard/warm.py)."""
    today = timezone.localdate()
    days = 30
    frm, to = bankflow_intelligence.resolve_range(days)
    for staff in (False, True):
        cache_aside(f"ui:readiness:{days}:{today}:{int(staff)}", ttl=_ttl(),
                    fn=partial(readiness.data_readiness, days=days, today=today, is_staff=staff))
        cache_aside(f"ui:review:{days}:{today}:{int(staff)}", ttl=_ttl(),
                    fn=partial(review.review_queue, days, is_staff=staff, today=today))
    cache_aside(f"ui:readiness:{days}:{today}", ttl=_ttl(),
                fn=lambda: readiness.data_readiness(days=days, today=today))
    cache_aside(f"ui:review:{days}:{today}", ttl=_ttl(),
                fn=lambda: review.review_queue(days, today=today))
    cache_aside(f"ui:outlook:{today}", ttl=_ttl(),
                fn=lambda: outlook.month_end_outlook(limit=0, today=today))
    cache_aside(f"ui:exc:{days}", ttl=_ttl(),
                fn=lambda: bankflow_intelligence.exceptions(frm, to, limit=200))
    cache_aside(f"ui:csp_rows:{today}", ttl=_ttl(), fn=lambda: _csp_rows(today))
