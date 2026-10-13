"""
Dashboard intelligence layer: PMJDY account growth -> transaction flow -> estimated Avg Balance
-> CSP actions. One set of functions, called identically by the API (api/v1/bankflow.py) and the
dashboard views, so the two can never disagree.

Four things are kept strictly apart in every payload:
  observed Avg Balance   SBI value from the Calling Sheet (csp.DailyBalance) -- never altered here
  account count          latest PMJDY observation (fallback: Calling Sheet count, labelled)
  transaction flow       attributed (exact KO match) pool credits/debits from bankflow.FlowAggregate
  estimate               bankflow.DailyEstimate -- model output with its own status

Flow naming: "credits"/"debits" are POOL-account directions (verified from the statement). Which of
them is a customer deposit or withdrawal depends on the channel mapping, which is still unconfirmed,
so the headline figures are pool credits, pool debits and net pool flow (credits - debits); the
provisional channel split is reported separately and labelled as such.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from csp.models import Csp, DailyBalance
from csp.models import Transaction as CspTransaction
from csp.rules import ELIGIBILITY_MIN_ACCOUNTS
from csp.services import get_data_freshness
from django.conf import settings
from django.db.models import Count, F, Max, Sum
from django.db.models.functions import TruncHour
from django.utils import timezone

from bankflow import flows, pmjdy, pmjdy_monthly
from bankflow.models import (
    AccountCountObservation,
    DailyEstimate,
    FlowAggregate,
    HypothesisRun,
    PmjdyMonthly,
    UnresolvedFlowDaily,
)

ZERO = Decimal(0)
PMJDY_STALE_HOURS = 26
GROWTH_ALERT_PCT = 5.0  # account growth over the range that should be backed by flow evidence
FLOW_COVERAGE_OK = 0.8
NOT_OBSERVATIONS = [DailyBalance.Source.DEMO, DailyBalance.Source.CARRY_FORWARD]
MAX_RANGE_DAYS = 366


def resolve_range(
    days: int = 30, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> tuple[dt.date, dt.date]:
    to = date_to or timezone.localdate()
    frm = date_from or (to - dt.timedelta(days=max(1, days) - 1))
    if frm > to:
        frm, to = to, frm
    if (to - frm).days >= MAX_RANGE_DAYS:
        frm = to - dt.timedelta(days=MAX_RANGE_DAYS - 1)
    return frm, to


def _f(v):
    return None if v is None else float(v)


def plain(value):
    """JSON-ready copy: Decimal -> float, date/datetime -> ISO string."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [plain(v) for v in value]
    return value


# ---------------------------------------------------------------- shared loaders
def _tracked() -> dict[str, Csp]:
    return {c.csp_code: c for c in Csp.objects.tracked()}


def _sheet_counts_as_of(date: dt.date, ids) -> dict[str, tuple[int | None, dt.date]]:
    rows = (
        DailyBalance.objects.filter(balance_date__lte=date, csp_id__in=list(ids),
                                    account_count__isnull=False)
        .exclude(source__in=NOT_OBSERVATIONS)
        .order_by("csp_id", "-balance_date")
        .distinct("csp_id")
        .values_list("csp_id", "account_count", "balance_date")
    )
    return {c: (n, d) for c, n, d in rows}


def account_counts(ids, date_from: dt.date, date_to: dt.date) -> dict[str, dict]:
    """Current (latest) and previous (start-of-range) account count per CSP.
    PMJDY is authoritative; the Calling Sheet count is used only where PMJDY has no reading,
    and is labelled `calling_sheet`. The current count is always the NEWEST reading."""
    ids = list(ids)
    cur = pmjdy.latest_counts(ids)
    prev = pmjdy.counts_as_of(date_from - dt.timedelta(days=1), ids)
    first_in_range: dict[str, tuple[int, dt.date]] = {}
    missing_prev = [c for c in ids if c in cur and c not in prev]
    if missing_prev:
        rows = (
            AccountCountObservation.objects.filter(
                source=pmjdy.SOURCE, csp_id__in=missing_prev, snapshot_date__gte=date_from
            )
            .order_by("csp_id", "observed_at").distinct("csp_id")
            .values_list("csp_id", "account_count", "snapshot_date")
        )
        first_in_range = {c: (n, d) for c, n, d in rows}
    need_sheet = [c for c in ids if c not in cur]
    sheet_cur = _sheet_counts_as_of(date_to, need_sheet) if need_sheet else {}
    sheet_prev = (
        _sheet_counts_as_of(date_from - dt.timedelta(days=1), need_sheet) if need_sheet else {}
    )
    out: dict[str, dict] = {}
    for c in ids:
        if c in cur:
            n, obs_at = cur[c]
            if c in prev:
                pn, pd_ = prev[c][0], prev[c][1].date()
            elif c in first_in_range and first_in_range[c][1] != timezone.localtime(obs_at).date():
                pn, pd_ = first_in_range[c]
            elif c in first_in_range:
                pn, pd_ = None, None  # only today's reading exists: no history to compare with
            else:
                pn, pd_ = None, None
            out[c] = {"current": n, "previous": pn, "previous_as_of": pd_, "source": "pmjdy",
                      "observed_at": obs_at}
        elif c in sheet_cur:
            n_sheet, _d = sheet_cur[c]
            sp = sheet_prev.get(c, (None, None))
            out[c] = {"current": n_sheet, "previous": sp[0], "previous_as_of": sp[1],
                      "source": "calling_sheet", "observed_at": None}
    return out


# Only these statement categories feed the estimate (flows.DEFAULT_DEFINITION), so only they make
# up the headline money in/out and each CSP's flow. Every other category (transfers, off-us
# withdrawals, loan/initial/SSA deposits, unrecognised channels) is reported separately as "other
# types": never added to the headline, never hidden.
COUNTED_CATEGORIES: tuple[str, ...] = flows.FLOW_DEFINITIONS[flows.DEFAULT_DEFINITION][1] or ()


def _split_flow(cats: dict | None) -> tuple[tuple, tuple]:
    """(credit, debit, count) of the counted categories and of all the others, from one
    FlowAggregate.by_category."""
    counted: list = [ZERO, ZERO, 0]
    other: list = [ZERO, ZERO, 0]
    for cat, v in (cats or {}).items():
        t = counted if cat in COUNTED_CATEGORIES else other
        t[0] += Decimal(v["credit"])
        t[1] += Decimal(v["debit"])
        t[2] += int(v["count"])
    return tuple(counted), tuple(other)


def _flow_rows(date_from: dt.date, date_to: dt.date):
    """Attributed (exact KO match) flow per CSP and day, as
    (csp, day, credit, debit, n, by_category, other) where credit/debit/n are the counted
    categories only and `other` is the (credit, debit, n) of everything else."""
    rows = FlowAggregate.objects.filter(
        resolution_mode=flows.MODE_EXACT, flow_date__gte=date_from, flow_date__lte=date_to
    ).values_list("csp_id", "flow_date", "by_category")
    for c, d, cats in rows.iterator():
        (cr, db, n), other = _split_flow(cats)
        yield c, d, cr, db, n, cats, other


def flow_by_csp(ids, date_from: dt.date, date_to: dt.date) -> dict[str, dict]:
    ids = set(ids)
    out: dict[str, dict] = {}
    for c, d, cr, db, n, _cat, _other in _flow_rows(date_from, date_to):
        if c not in ids:
            continue
        r = out.setdefault(c, {"credits": ZERO, "debits": ZERO, "txn_count": 0, "days": set()})
        r["credits"] += cr
        r["debits"] += db
        r["txn_count"] += n
        r["days"].add(d)
    return out


def latest_observed(ids) -> dict[str, dict]:
    rows = (
        DailyBalance.objects.filter(csp_id__in=list(ids)).exclude(source__in=NOT_OBSERVATIONS)
        .order_by("csp_id", "-balance_date").distinct("csp_id")
        .values_list("csp_id", "daily_avg_balance", "balance_date")
    )
    return {c: {"avg": v, "last_seen": d} for c, v, d in rows}


def latest_estimate_date() -> dt.date | None:
    return DailyEstimate.objects.aggregate(m=Max("estimate_date"))["m"]


def estimates_by_csp(estimate_date: dt.date | None) -> dict[str, DailyEstimate]:
    if estimate_date is None:
        return {}
    return {e.csp_id: e for e in DailyEstimate.objects.filter(estimate_date=estimate_date)}


# ---------------------------------------------------------------- freshness
def source_freshness() -> dict:
    now = timezone.now()
    fr = get_data_freshness()
    last_pm = PmjdyMonthly.objects.aggregate(m=Max("observed_at"))["m"]
    last_flow = FlowAggregate.objects.filter(resolution_mode=flows.MODE_EXACT).aggregate(
        d=Max("flow_date"), u=Max("updated_at"))
    est = DailyEstimate.objects.aggregate(d=Max("estimate_date"), c=Max("calculated_at"))
    sheet_at = fr.get("calling_sheet_last_success")

    def _age(ts):
        return None if ts is None else round((now - ts).total_seconds() / 3600, 1)

    def _state(age_h, limit):
        return "missing" if age_h is None else ("fresh" if age_h <= limit else "stale")

    pm_age, sheet_age, est_age = _age(last_pm), _age(sheet_at), _age(est["c"])
    return {
        "calling_sheet": {"last_success": sheet_at, "age_hours": sheet_age,
                          "state": fr.get("overall_status", _state(sheet_age, 26))},
        "pmjdy": {"last_observed": last_pm, "age_hours": pm_age,
                  "state": _state(pm_age, PMJDY_STALE_HOURS)},
        "transactions": {"last_flow_date": last_flow["d"], "last_processed": last_flow["u"],
                         "age_hours": _age(last_flow["u"]),
                         "state": _state(_age(last_flow["u"]), 48)},
        "estimates": {"estimate_date": est["d"], "calculated_at": est["c"], "age_hours": est_age,
                      "state": _state(est_age, 26)},
    }


def estimator_status() -> dict:
    run = HypothesisRun.objects.first()
    if run is None:
        return {"status": "NOT_RUN", "selected_method": "carry_forward",
                "detail": "No historical evaluation has been run yet.", "run_at": None}
    return {"status": run.status, "selected_method": run.selected_method, "detail": run.detail,
            "run_at": run.created_at, "n_pairs": run.n_pairs, "n_events": run.n_events}


# ---------------------------------------------------------------- summary
def _new_accounts(day: dt.date) -> dict:
    """New PMJDY accounts opened so far in `day`'s month (from the sheet's monthly blocks)."""
    month = f"{day:%Y-%m}"
    t = pmjdy_monthly.monthly_totals(month)
    base = {"month": month, "month_label": day.strftime("%B %Y"), "available": t is not None}
    if t is None:
        return {**base, "mtd": None, "today": None, "target": None, "csps_reporting": 0,
                "observed_at": None}
    return {**base, "mtd": t["mtd"], "today": t["ftd"], "target": t["target"],
            "csps_reporting": t["csps_reporting"], "observed_at": t["observed_at"]}


def dashboard_summary(date_from: dt.date, date_to: dt.date) -> dict:
    csps = _tracked()
    ids = list(csps)
    counts = account_counts(ids, date_from, date_to)
    with_prev = [c for c in counts.values() if c["previous"] is not None]
    total_now = sum(c["current"] for c in counts.values())
    growth = sum(c["current"] - c["previous"] for c in with_prev) if with_prev else None
    sources = {s: sum(1 for c in counts.values() if c["source"] == s)
               for s in ("pmjdy", "calling_sheet")}

    credits = debits = other_cr = other_db = ZERO
    txns = other_n = 0
    days: set[dt.date] = set()
    cat_split: dict[str, dict] = defaultdict(lambda: {"credit": ZERO, "debit": ZERO})
    for _c, d, cr, db, n, cats, other in _flow_rows(date_from, date_to):
        credits += cr
        debits += db
        txns += n
        other_cr += other[0]
        other_db += other[1]
        other_n += other[2]
        days.add(d)
        for cat, v in (cats or {}).items():
            cat_split[cat]["credit"] += Decimal(v["credit"])
            cat_split[cat]["debit"] += Decimal(v["debit"])
    range_days = (date_to - date_from).days + 1
    complete = {d for d in flows.proven_complete_dates() if date_from <= d <= date_to}
    unresolved = UnresolvedFlowDaily.objects.filter(
        flow_date__gte=date_from, flow_date__lte=date_to
    )
    unres_amt = unresolved.aggregate(c=Sum("credit_amount"), d=Sum("debit_amount"))
    unres_total = (unres_amt["c"] or ZERO) + (unres_amt["d"] or ZERO)
    # the unattributed share compares like with like: statement value of ALL types that could not
    # be linked to a CSP against statement value of ALL types that could
    attributed_all = credits + debits + other_cr + other_db

    obs = latest_observed(ids)
    est_date = latest_estimate_date()
    est = estimates_by_csp(est_date)
    status_counts: dict[str, int] = defaultdict(int)
    for e in est.values():
        status_counts[e.status] += 1
    with_est = sum(1 for e in est.values() if e.status != "UNAVAILABLE")
    ineligible = sum(1 for c in counts.values() if c["current"] < ELIGIBILITY_MIN_ACCOUNTS)

    # network-level per-account averages, weighted by each CSP's current account count
    def _weighted(pairs):
        pairs = [(v, n) for v, n in pairs if v is not None and n]
        tot = sum(n for _, n in pairs)
        return (sum(v * n for v, n in pairs) / tot, len(pairs)) if tot else (None, 0)

    obs_avg, obs_n = _weighted(
        (o["avg"], counts[c]["current"]) for c, o in obs.items() if c in counts
    )
    model_rows = [
        e for e in est.values()
        if e.status in ("VALIDATED", "PROVISIONAL") and e.estimated_value is not None
        and e.current_account_count
    ]
    est_avg, est_n = _weighted((e.estimated_value, e.current_account_count) for e in model_rows)
    base_avg, _ = _weighted((e.baseline_value, e.current_account_count) for e in model_rows)
    network_estimate = None
    if est_avg is not None:
        network_estimate = {
            "value": est_avg, "baseline": base_avg, "csps": est_n,
            "status": "VALIDATED" if all(e.status == "VALIDATED" for e in model_rows)
            else "PROVISIONAL",
        }
    return {
        "range": {"from": date_from, "to": date_to, "days": range_days},
        "csps_tracked": len(ids),
        "new_accounts": _new_accounts(date_to),
        "accounts": {
            "current_total": total_now, "growth": growth,
            "growth_pct": (growth / (total_now - growth) * 100) if growth is not None
            and total_now - growth > 0 else None,
            "csps_with_history": len(with_prev), "csps_reporting": len(counts),
            "by_source": sources,
            "source": (
                "PMJDY tab (latest reading)"
                if sources["pmjdy"]
                else "Calling Sheet (PMJDY not yet captured)"
            ),
        },
        "flows": {
            "pool_credits": credits, "pool_debits": debits, "net_pool_flow": credits - debits,
            "gross_flow": credits + debits, "txn_count": txns,
            "days_with_data": len(days), "proven_complete_days": len(complete),
            "range_days": range_days,
            "coverage": len(days) / range_days if range_days else 0,
            "attribution": "exact KO match only",
            "unattributed_amount": unres_total,
            "unattributed_share": _f(unres_total / (unres_total + attributed_all))
            if (unres_total + attributed_all) else None,
            "counted_types": list(COUNTED_CATEGORIES),
            "other_types": {
                "credit": other_cr, "debit": other_db, "txn_count": other_n,
                "note": "Money transfers, other-bank (off-us) withdrawals, loan/initial/SSA "
                        "deposits and unrecognised channels: shown here, not in the totals above.",
            },
            "provisional_channel_split": {
                k: {"credit": v["credit"], "debit": v["debit"]} for k, v in cat_split.items()
            },
            "split_note": "Only withdrawal and deposit transactions are counted in the totals. "
                          "The channel->deposit/withdrawal mapping is unconfirmed; pool "
                          "credit/debit direction is what the statement states.",
        },
        "observed_avg_balance": {
            "csps_with_observation": len(obs), "coverage": len(obs) / len(ids) if ids else None,
            "network_per_account_avg": obs_avg,
            "source": "SBI Avg Balance (Calling Sheet)",
            "latest_observation_date": max((o["last_seen"] for o in obs.values()), default=None),
        },
        "estimates": {
            "estimate_date": est_date, "csps_with_estimate": with_est,
            "coverage": with_est / len(ids) if ids else None,
            "by_status": dict(status_counts), "below_200_account_gate": ineligible,
            "network_estimate": network_estimate,
            "estimator": estimator_status(),
        },
        "freshness": source_freshness(),
    }


# ---------------------------------------------------------------- movement
def network_movement(date_from: dt.date, date_to: dt.date) -> dict:
    by_day: dict[dt.date, dict] = {}
    for _c, d, cr, db, n, _cat, _other in _flow_rows(date_from, date_to):
        r = by_day.setdefault(d, {"credits": ZERO, "debits": ZERO, "txn_count": 0})
        r["credits"] += cr
        r["debits"] += db
        r["txn_count"] += n
    complete = flows.proven_complete_dates()
    acct = {r["date"]: r for r in pmjdy.daily_network_counts(date_from, date_to)}
    acct_source = "pmjdy" if any(r["total_accounts"] is not None for r in acct.values()) else None

    obs = {
        r["balance_date"]: r for r in DailyBalance.objects.filter(
            balance_date__gte=date_from, balance_date__lte=date_to, account_count__isnull=False
        ).exclude(source__in=NOT_OBSERVATIONS).values("balance_date").annotate(
            w=Sum(F("daily_avg_balance") * F("account_count")), n=Sum("account_count"),
            c=Count("id"))
    }
    est = {
        r["estimate_date"]: r for r in DailyEstimate.objects.filter(
            estimate_date__gte=date_from, estimate_date__lte=date_to,
            estimated_value__isnull=False, current_account_count__isnull=False
        ).values("estimate_date").annotate(
            ew=Sum(F("estimated_value") * F("current_account_count")),
            bw=Sum(F("baseline_value") * F("current_account_count")),
            n=Sum("current_account_count"), c=Count("id"))
    }
    if acct_source is None:  # PMJDY not captured yet: Calling Sheet totals, clearly labelled
        acct_source = "calling_sheet"
        for d, r in obs.items():
            if d in acct:
                acct[d] = {"date": d, "total_accounts": r["n"], "csps_reporting": r["c"],
                           "growth": None}
    series = []
    day = date_from
    while day <= date_to:
        f = by_day.get(day)
        a = acct.get(day, {})
        o, e = obs.get(day), est.get(day)
        series.append({
            "date": day,
            "pool_credits": f["credits"] if f else None,
            "pool_debits": f["debits"] if f else None,
            "net_pool_flow": (f["credits"] - f["debits"]) if f else None,
            "txn_count": f["txn_count"] if f else None,
            "flow_status": (
                "complete" if day in complete else ("partial_or_unverified" if f else "no_data")
            ),
            "total_accounts": a.get("total_accounts"),
            "account_growth": a.get("growth"),
            "observed_avg_balance": _f(o["w"] / o["n"]) if o and o["n"] else None,
            "estimated_avg_balance": _f(e["ew"] / e["n"]) if e and e["n"] else None,
            "baseline_for_estimate": _f(e["bw"] / e["n"]) if e and e["n"] else None,
        })
        day += dt.timedelta(days=1)
    return {
        "range": {"from": date_from, "to": date_to},
        "granularity": "daily",
        "series": series,
        "account_source": acct_source,
        "notes": [
            "Transaction flows come from statements dated by day (no time of day): daily only.",
            "SBI Avg Balance changes about monthly per CSP: the observed line is stepwise, "
            "not a daily measurement.",
            "Estimated points exist only for days the estimator ran; they are model output, "
            "not balances.",
        ],
    }


def hourly_activity(hours: int = 48) -> dict:
    """Hourly activity from the verified transaction sheet (csp.Transaction): counts and
    amounts by category. Separate from the daily statement flows above."""
    since = timezone.now() - dt.timedelta(hours=max(1, min(hours, 24 * 14)))
    rows = (
        CspTransaction.objects.filter(txn_datetime__gte=since)
        .annotate(h=TruncHour("txn_datetime")).values("h", "category")
        .annotate(n=Count("ref_number"), amt=Sum("amount")).order_by("h")
    )
    return {"granularity": "hourly", "since": since,
            "series": [{"hour": r["h"], "category": r["category"], "count": r["n"],
                        "amount": r["amt"]} for r in rows],
            "source": "Transaction sheet (csp.Transaction)"}


# ---------------------------------------------------------------- growth explorer
SORTS = {
    "csp_code": lambda r: r["csp_code"],
    "name": lambda r: (r["name"] or "").lower(),
    "count_change": lambda r: r["count_change"] if r["count_change"] is not None else -10**9,
    "growth_pct": lambda r: r["growth_pct"] if r["growth_pct"] is not None else -10**9,
    "current_count": lambda r: r["current_count"] or 0,
    "credits": lambda r: r["credits"] or ZERO,
    "debits": lambda r: r["debits"] or ZERO,
    "net_flow": lambda r: r["net_flow"] if r["net_flow"] is not None else Decimal(-(10**15)),
    "estimated_delta": lambda r: (
        r["estimated_delta"] if r["estimated_delta"] is not None else Decimal(-(10**15))
    ),
    "observed_avg": lambda r: r["observed_avg"] or ZERO,
}


def _build_rows(date_from: dt.date, date_to: dt.date) -> tuple[list[dict], dict]:
    csps = _tracked()
    ids = list(csps)
    counts = account_counts(ids, date_from, date_to)
    fl = flow_by_csp(ids, date_from, date_to)
    complete = {d for d in flows.proven_complete_dates() if date_from <= d <= date_to}
    est_date = latest_estimate_date()
    est = estimates_by_csp(est_date)
    obs = latest_observed(ids) if not est else {}
    pm = pmjdy_monthly.latest_monthly(f"{date_to:%Y-%m}", ids)
    range_days = (date_to - date_from).days + 1
    now = timezone.now()
    rows = []
    for code, csp in csps.items():
        a = counts.get(code)
        f = fl.get(code)
        e = est.get(code)
        cur = a["current"] if a else None
        prev = a["previous"] if a else None
        change = (cur - prev) if cur is not None and prev is not None else None
        growth_pct = (change / prev * 100) if change is not None and prev else None
        o_avg = e.baseline_value if e else (obs.get(code) or {}).get("avg")
        o_seen = e.baseline_date if e else None
        last_seen = e.last_seen_date if e else (obs.get(code) or {}).get("last_seen")
        if a and a["observed_at"]:
            age_h = (now - a["observed_at"]).total_seconds() / 3600
            fresh = "fresh" if age_h <= PMJDY_STALE_HOURS else "stale"
        elif a:
            fresh = "calling_sheet_only"
        else:
            fresh = "no_data"
        rows.append({
            "csp_code": code, "name": csp.name, "zone": csp.zone,
            "previous_count": prev, "previous_as_of": a["previous_as_of"] if a else None,
            "current_count": cur, "count_change": change, "growth_pct": growth_pct,
            "count_source": a["source"] if a else None,
            "count_observed_at": a["observed_at"] if a else None, "count_freshness": fresh,
            "credits": f["credits"] if f else None, "debits": f["debits"] if f else None,
            "net_flow": (f["credits"] - f["debits"]) if f else None,
            "gross_flow": (f["credits"] + f["debits"]) if f else None,
            "txn_count": f["txn_count"] if f else None,
            "flow_days": len(f["days"]) if f else 0,
            "flow_coverage": (len(f["days"]) / range_days) if f else 0.0,
            "flow_proven_complete_days": len(f["days"] & complete) if f else 0,
            "observed_avg": o_avg, "observed_first_seen": o_seen, "observed_last_seen": last_seen,
            "estimated_value": e.estimated_value if e else None,
            "estimated_delta": e.estimated_delta if e else None,
            "estimate_status": e.status if e else "NOT_COMPUTED",
            "estimate_reason": e.reason_code if e else "ESTIMATES_NOT_RUN",
            "estimate_method": e.method if e else None,
            "expected_error_pct": e.expected_error_pct if e else None,
            "estimate_stale": bool(e and e.details.get("stale")),
            "estimate_calculated_at": e.calculated_at if e else None,
            "pmjdy_mtd": pm[code].mtd if code in pm else None,
            "pmjdy_target": pm[code].target if code in pm else None,
        })
    active_days: set[dt.date] = set()
    for f in fl.values():
        active_days |= f["days"]
    return rows, {
        "estimate_date": est_date, "range_days": range_days,
        "network_flow_coverage": len(active_days) / range_days if range_days else 0.0,
    }


def csp_growth(
    date_from: dt.date, date_to: dt.date, *, search: str = "", status: str = "", flag: str = "",
    sort: str = "net_flow", descending: bool = True, limit: int = 25, offset: int = 0,
) -> tuple[list[dict], int, dict]:
    rows, meta = _build_rows(date_from, date_to)
    flags = csp_flags(rows, meta["network_flow_coverage"])
    for r in rows:
        r["flags"] = flags.get(r["csp_code"], [])
    q = search.strip().lower()
    if q:
        rows = [r for r in rows if q in r["csp_code"].lower() or q in (r["name"] or "").lower()]
    if status:
        rows = [r for r in rows if r["estimate_status"] == status]
    if flag:
        rows = [r for r in rows if flag in {f["type"] for f in r["flags"]}]
    key = SORTS.get(sort, SORTS["net_flow"])
    rows.sort(key=key, reverse=descending)
    return rows[offset: offset + limit], len(rows), meta


# ---------------------------------------------------------------- exceptions
SEVERITY = {"high": 0, "medium": 1, "low": 2}


def csp_flags(rows: list[dict], network_coverage: float = 1.0) -> dict[str, list[dict]]:
    """Per-CSP exceptions, each with the evidence that triggered it and a next action.

    Flow alerts are about MATERIALITY: an outflow is flagged only when it is large in rupees and a
    large share of the CSP's own activity (thresholds: settings.ALERT_*), and only when statements
    cover enough of the period for the comparison to mean anything."""
    out: dict[str, list[dict]] = defaultdict(list)
    min_out = Decimal(str(settings.ALERT_MIN_OUTFLOW))
    high_out = Decimal(str(settings.ALERT_HIGH_OUTFLOW))
    min_ratio, high_ratio = settings.ALERT_MIN_IMBALANCE, settings.ALERT_HIGH_IMBALANCE
    evidence_ok = network_coverage >= settings.ALERT_MIN_NETWORK_COVERAGE

    def add(row, type_, sev, reason, action, rank=0.0, **metrics):
        out[row["csp_code"]].append({
            "type": type_, "severity": sev, "reason": reason, "next_action": action, "rank": rank,
            "metrics": metrics, "evidence_coverage": row["flow_coverage"],
        })

    for r in rows:
        net, gross = r["net_flow"], r["gross_flow"]
        if evidence_ok and net is not None and net < 0 and gross:
            outflow, ratio = -net, float(-net / gross)
            if outflow >= min_out and ratio >= min_ratio:
                high = outflow >= high_out and ratio >= high_ratio
                add(r, "NEGATIVE_NET_FLOW", "high" if high else "medium",
                    f"Pool debits exceeded pool credits by {ratio:.0%} of this CSP's activity "
                    "in the range. This is a pool-direction fact, not a measured change in "
                    "customer balances.",
                    "Review recent activity with the CSP / territory lead before concluding.",
                    rank=float(outflow), net_flow=_f(net), credits=_f(r["credits"]),
                    debits=_f(r["debits"]), imbalance_pct=round(ratio * 100, 1))
        new_n = r["pmjdy_mtd"] if r["pmjdy_mtd"] is not None else r["count_change"]
        if (evidence_ok and new_n is not None and new_n >= settings.ALERT_NEW_ACCOUNTS
                and r["flow_days"] == 0):
            add(r, "GROWTH_WITHOUT_FLOW_EVIDENCE", "medium",
                f"{new_n} new accounts but no attributed money movement at all in the range.",
                "Check that statements for these days are ingested and the KO code is matching.",
                rank=float(new_n), new_accounts=new_n)
        if r["count_change"] is not None and r["count_change"] < 0:
            add(r, "ACCOUNT_COUNT_DECREASED", "high",
                "Total accounts are lower than at the start of the range; counts are expected "
                "not to fall.",
                "Verify this CSP's Total Accounts entry in the Calling Sheet.",
                rank=float(-r["count_change"]), previous=r["previous_count"],
                current=r["current_count"])
        if (r["estimate_status"] in ("VALIDATED", "PROVISIONAL")
                and r["estimated_delta"] is not None and r["estimated_delta"] < 0):
            add(r, "POSSIBLE_ESTIMATED_DECLINE",
                "high" if r["estimate_status"] == "VALIDATED" else "medium",
                f"{r['estimate_status'].title()} estimate is below the last SBI value "
                f"(method: {r['estimate_method']}). An estimate, not an observed balance.",
                "Confirm with the CSP; wait for the next SBI refresh to verify.",
                rank=float(-r["estimated_delta"]), estimated_delta=_f(r["estimated_delta"]),
                baseline=_f(r["observed_avg"]))
        if r["count_freshness"] in ("stale", "no_data") or r["estimate_stale"]:
            add(r, "STALE_OR_MISSING_SOURCE", "low",
                {"stale": "The account count reading is older than 26 hours.",
                 "no_data": "No account count has been captured for this CSP."}.get(
                    r["count_freshness"], "Calling Sheet observation is older than 3 days."),
                "Check the Calling Sheet ingest for this CSP.",
                freshness=r["count_freshness"])
    return out


def exceptions(date_from: dt.date, date_to: dt.date, limit: int = 100) -> dict:
    rows, meta = _build_rows(date_from, date_to)
    per = csp_flags(rows, meta["network_flow_coverage"])
    by_code = {r["csp_code"]: r for r in rows}
    items = []
    for code, fl in per.items():
        r = by_code[code]
        for f in fl:
            items.append({"csp_code": code, "name": r["name"], **f})
    # network-level items
    s = dashboard_summary(date_from, date_to)
    fl_ = s["flows"]
    if fl_["unattributed_share"] is not None and fl_["unattributed_share"] > 0.05:
        items.append({
            "csp_code": None, "name": "Network", "type": "UNATTRIBUTED_FLOW", "severity": "medium",
            "reason": (
                f"{fl_['unattributed_share']:.0%} of statement value in the range could not be "
                "attributed to a CSP (unmatched / ambiguous / uncoded). It is excluded from "
                "every CSP figure."
            ),
            "next_action": "Review code-resolution; do not enable unverified KO rules.",
            "metrics": {"unattributed_amount": _f(fl_["unattributed_amount"])},
            "evidence_coverage": fl_["coverage"]})
    est = s["estimates"]
    insufficient = est["by_status"].get("INSUFFICIENT_EVIDENCE", 0)
    if insufficient:
        items.append({
            "csp_code": None, "name": "Network", "type": "INSUFFICIENT_HISTORY", "severity": "low",
            "reason": f"{insufficient} CSP estimate(s) are carry-forward only; the estimator is "
                      f"{est['estimator']['status']}: {est['estimator']['detail']}",
            "next_action": "Load Calling Sheet history and statements overlapping SBI refreshes.",
            "metrics": {"csps": insufficient}, "evidence_coverage": fl_["coverage"]})
    if meta["network_flow_coverage"] < settings.ALERT_MIN_NETWORK_COVERAGE:
        items.append({
            "csp_code": None, "name": "Network", "type": "STATEMENT_COVERAGE_LOW",
            "severity": "low", "rank": 0.0,
            "reason": (
                f"Bank statements cover only {meta['network_flow_coverage']:.0%} of the days in "
                "this period, so alerts about money going out are paused."),
            "next_action": "Load the missing statements, then alerts resume on their own.",
            "metrics": {"coverage": round(meta["network_flow_coverage"], 3)},
            "evidence_coverage": meta["network_flow_coverage"]})
    items.sort(key=lambda i: (SEVERITY[i["severity"]], -i.get("rank", 0.0)))  # biggest first
    counts: dict[str, int] = defaultdict(int)
    for i in items:
        counts[i["type"]] += 1
    return {"range": {"from": date_from, "to": date_to}, "total": len(items),
            "by_type": dict(counts), "items": items[:limit]}
