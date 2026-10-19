"""
Dashboard intelligence layer: PMJDY account growth -> transaction flow -> estimated Avg Balance
-> CSP actions. One set of functions, called identically by the API (api/v1/bankflow.py) and the
dashboard views, so the two can never disagree.

Four things are kept strictly apart in every payload:
  observed Avg Balance   SBI value from the Calling Sheet (csp.DailyBalance) -- never altered here
  account count          latest PMJDY observation (fallback: Calling Sheet count, labelled)
  transaction flow       attributed (exact KO match) pool credits/debits from bankflow.FlowAggregate
  estimate               bankflow.DailyEstimate -- model output with its own status

Flow naming (direction CONFIRMED by the business seniors, 2026-10-14): a pool CREDIT is a customer
WITHDRAWAL (cash the CSP paid out) and a pool DEBIT is a customer DEPOSIT (cash the CSP took in).
`withdrawals`, `deposits` and `net_customer_flow` (= deposits - withdrawals; positive means customer
money grew) are the figures to show. `pool_credits`, `pool_debits` and `net_pool_flow` (credits -
debits) are kept as the raw pool-direction fields. Only withdrawal and deposit rows are counted.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any, NamedTuple, cast

from common.cache import cache_aside
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


def _memo(key: str, fn):
    """Cache one expensive building block for the current data generation (see common/cache.py):
    anything that writes the data it reads bumps the generation, so a long TTL costs no
    freshness."""
    return cache_aside(key, ttl=settings.DASHBOARD_CACHE_TTL, fn=fn)


def _latest_readings(day: dt.date | None, *, with_count: bool) -> dict[str, tuple]:
    """{csp: (SBI average balance, account count, reading date)}: each CSP's newest observed
    reading on or before `day` (None = the newest there is), demo and carry-forward rows excluded,
    optionally only readings that carry an account count.

    One index probe per CSP (LATERAL) instead of sorting the whole balance history for every
    question, and cached: this was asked 6-16 times a page, ~35 ms each and growing with history."""
    def compute():
        from django.db import connection

        extra = "AND b.account_count IS NOT NULL" if with_count else ""
        sql = f"""
            SELECT c.csp_code, l.daily_avg_balance, l.account_count, l.balance_date
            FROM {Csp._meta.db_table} c
            JOIN LATERAL (
                SELECT b.daily_avg_balance, b.account_count, b.balance_date
                FROM {DailyBalance._meta.db_table} b
                WHERE b.csp_id = c.csp_code AND b.balance_date <= %s {extra}
                  AND b.source <> ALL(%s)
                ORDER BY b.balance_date DESC LIMIT 1
            ) l ON TRUE
        """
        with connection.cursor() as cur:
            cur.execute(sql, [day or dt.date.max, [str(x) for x in NOT_OBSERVATIONS]])
            return {c: (avg, n, d) for c, avg, n, d in cur.fetchall()}

    return _memo(f"bf:latest_readings:{day}:{int(with_count)}", compute)


def _sheet_counts_as_of(date: dt.date, ids) -> dict[str, tuple[int | None, dt.date]]:
    snap = _latest_readings(date, with_count=True)
    return {c: (snap[c][1], snap[c][2]) for c in ids if c in snap}


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
# up the headline withdrawals/deposits and each CSP's flow. Every other category (transfers, off-us
# withdrawals, loan/initial/SSA deposits, unrecognised channels) is reported separately as "other
# types": never added to the headline, never hidden.
COUNTED_CATEGORIES: tuple[str, ...] = flows.FLOW_DEFINITIONS[flows.DEFAULT_DEFINITION][1] or ()


def _split_flow(cats: dict | None) -> tuple[dict, tuple]:
    """One FlowAggregate.by_category split into the counted categories (with the customer
    withdrawals and deposits) and everything else."""
    counted: dict[str, Any] = {"credit": ZERO, "debit": ZERO, "n": 0, "withdrawals": ZERO,
                               "deposits": ZERO}
    other: list = [ZERO, ZERO, 0]
    for cat, v in (cats or {}).items():
        cr, db, k = Decimal(v["credit"]), Decimal(v["debit"]), int(v["count"])
        if cat in COUNTED_CATEGORIES:
            counted["credit"] += cr
            counted["debit"] += db
            counted["n"] += k
            if cat == "withdrawal":
                counted["withdrawals"] += cr - db
            elif cat == "deposit":
                counted["deposits"] += db - cr
        else:
            other[0] += cr
            other[1] += db
            other[2] += k
    return counted, tuple(other)


class FlowGroup(NamedTuple):
    """The counted-category figures of one group of FlowAggregate rows (a CSP, a day, or all)."""
    credit: Decimal        # pool credits of the counted categories
    debit: Decimal         # pool debits of the counted categories
    n: int                 # transactions in the counted categories
    cats: dict             # {category: {"credit", "debit", "count"}} for every category
    other: tuple           # (credit, debit, n) of every category that is not counted
    withdrawals: Decimal   # cash customers took out, net of reversals
    deposits: Decimal      # cash customers put in, net of reversals
    days: frozenset        # the flow dates that contributed


_FLOW_GROUP_KEY = {"csp": "fa.csp_id", "day": "fa.flow_date", "all": None}


def _flow_groups(date_from: dt.date, date_to: dt.date, ids=None, *, by: str) -> dict:
    """Attributed (exact KO match) flow, grouped `by` "csp", "day" or "all" (key None), summed in
    the database from FlowAggregate.by_category. A 30-day range is ~16,000 aggregate rows with a
    JSON breakdown each; decoding and folding those in Python cost ~0.2 s a page, while the
    grouped result is a few hundred rows. `ids` limits it to those CSPs (the dashboard passes the
    tracked ones)."""
    from django.db import connection

    key = _FLOW_GROUP_KEY[by]
    select_key = key or "NULL"                      # "all": one group, so no grouping column
    group_cat = f"{key}, e.key" if key else "e.key"
    group_days = f"GROUP BY {key}" if key else ""
    where = "fa.resolution_mode = %s AND fa.flow_date BETWEEN %s AND %s"
    params: list = [flows.MODE_EXACT, date_from, date_to]
    if ids is not None:
        where += " AND fa.csp_id = ANY(%s)"
        params.append(list(ids))
    table = FlowAggregate._meta.db_table
    with connection.cursor() as cur:
        cur.execute(
            f"""SELECT {select_key}, e.key, SUM((e.value->>'credit')::numeric),
                       SUM((e.value->>'debit')::numeric), SUM((e.value->>'count')::bigint)
                FROM {table} fa CROSS JOIN LATERAL jsonb_each(fa.by_category) AS e(key, value)
                WHERE {where} GROUP BY {group_cat}""", params)
        cats: dict = defaultdict(dict)
        for k, cat, cr, db, n in cur.fetchall():
            cats[k][cat] = {"credit": cr or ZERO, "debit": db or ZERO, "count": int(n or 0)}
        cur.execute(
            f"SELECT {select_key}, array_agg(DISTINCT fa.flow_date) FROM {table} fa "
            f"WHERE {where} {group_days}", params)
        days = {k: frozenset(d or ()) for k, d in cur.fetchall()}
    out = {}
    for k, per_cat in cats.items():
        counted, other = _split_flow(per_cat)
        out[k] = FlowGroup(counted["credit"], counted["debit"], counted["n"], per_cat, other,
                           counted["withdrawals"], counted["deposits"], days.get(k, frozenset()))
    return out


def flow_by_csp(ids, date_from: dt.date, date_to: dt.date) -> dict[str, dict]:
    return {
        csp: {"credits": g.credit, "debits": g.debit, "withdrawals": g.withdrawals,
              "deposits": g.deposits, "txn_count": g.n, "days": set(g.days)}
        for csp, g in _flow_groups(date_from, date_to, set(ids), by="csp").items()
    }


def latest_observed(ids) -> dict[str, dict]:
    snap = _latest_readings(None, with_count=False)
    return {c: {"avg": snap[c][0], "last_seen": snap[c][2]} for c in ids if c in snap}


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
    """Cached per data generation: Home, Money Movement, Needs Attention, the sidebar badge and the
    API all ask for the same range, and it used to be recomputed for each of them."""
    return _memo(f"bf:summary:{date_from}:{date_to}",
                 lambda: _dashboard_summary(date_from, date_to))


def _dashboard_summary(date_from: dt.date, date_to: dt.date) -> dict:
    csps = _tracked()
    ids = list(csps)
    counts = account_counts(ids, date_from, date_to)
    with_prev = [c for c in counts.values() if c["previous"] is not None]
    total_now = sum(c["current"] for c in counts.values())
    growth = sum(c["current"] - c["previous"] for c in with_prev) if with_prev else None
    sources = {s: sum(1 for c in counts.values() if c["source"] == s)
               for s in ("pmjdy", "calling_sheet")}

    credits = debits = other_cr = other_db = withdrawals = deposits = ZERO
    txns = other_n = 0
    days: set[dt.date] = set()
    cat_split: dict[str, dict] = defaultdict(lambda: {"credit": ZERO, "debit": ZERO})
    total = _flow_groups(date_from, date_to, ids, by="all").get(None)
    if total is not None:
        credits, debits, txns = total.credit, total.debit, total.n
        withdrawals, deposits = total.withdrawals, total.deposits
        other_cr, other_db, other_n = total.other
        days = set(total.days)
        for cat, v in total.cats.items():
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
            "withdrawals": withdrawals, "deposits": deposits,
            "net_customer_flow": deposits - withdrawals,
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
                          "A pool credit is a customer withdrawal and a pool debit a customer "
                          "deposit (confirmed by the business, 2026-10-14).",
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
    return _memo(f"bf:movement:{date_from}:{date_to}",
                 lambda: _network_movement(date_from, date_to))


def _network_movement(date_from: dt.date, date_to: dt.date) -> dict:
    by_day: dict[dt.date, dict] = {
        day: {"credits": g.credit, "debits": g.debit, "withdrawals": g.withdrawals,
              "deposits": g.deposits, "txn_count": g.n}
        for day, g in _flow_groups(date_from, date_to, _tracked(), by="day").items()
    }
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
            "withdrawals": f["withdrawals"] if f else None,
            "deposits": f["deposits"] if f else None,
            "net_customer_flow": (f["deposits"] - f["withdrawals"]) if f else None,
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


# ---------------------------------------------------------------- today vs yesterday
def _weighted_avg(pairs) -> tuple[Decimal | None, int]:
    pairs = [(v, n) for v, n in pairs if v is not None and n]
    tot = sum(n for _, n in pairs)
    return (sum(v * n for v, n in pairs) / tot, len(pairs)) if tot else (None, 0)


def _accounts_as_of(day: dt.date, ids) -> dict[str, int]:
    """{csp: customer-account count} as of the end of `day`: the newest PMJDY reading on or
    before it, else the newest Calling Sheet count (labelled elsewhere as the fallback)."""
    ids = list(ids)
    out = {c: n for c, (n, _at) in pmjdy.counts_as_of(day, ids).items()}
    rest = [c for c in ids if c not in out]
    if rest:
        out.update({c: n for c, (n, _d) in _sheet_counts_as_of(day, rest).items()
                    if n is not None})
    return out


def _monthly_readings_as_of(day: dt.date, ids) -> list:
    """The newest PmjdyMonthly reading of each CSP for `day`'s month, taken by the end of `day`."""
    from .progress import day_bounds

    _start, end = day_bounds(day)
    return list(
        PmjdyMonthly.objects.filter(month=f"{day:%Y-%m}", observed_at__lt=end,
                                    csp_id__in=list(ids))
        .order_by("csp_id", "-observed_at").distinct("csp_id")
    )


def _new_accounts_as_of(day: dt.date, ids) -> dict | None:
    """New accounts opened so far in `day`'s month (MTD) and that day's own figure (FTD), from the
    newest reading taken on or before the end of `day`."""
    rows = _monthly_readings_as_of(day, ids)
    if not rows:
        return None
    mtd = [r.mtd for r in rows if r.mtd is not None]
    ftd = [r.ftd for r in rows if r.ftd is not None]
    return {"mtd": sum(mtd) if mtd else None, "ftd": sum(ftd) if ftd else None,
            "csps": len(rows), "observed_at": max(r.observed_at for r in rows)}


def _observed_by_csp(day: dt.date, ids) -> dict[str, tuple]:
    """{csp: (SBI average balance, account count)}: each CSP's newest reading on or before `day`."""
    snap = _latest_readings(day, with_count=True)
    return {c: (snap[c][0], snap[c][1]) for c in ids if c in snap}


def _observed_avg_as_of(day: dt.date, ids) -> tuple[Decimal | None, int]:
    return _weighted_avg(_observed_by_csp(day, ids).values())


def _estimates_by_csp(day: dt.date, ids) -> dict[str, tuple]:
    """{csp: (estimated value, account count, status)} for that day's validated or provisional
    estimates only."""
    rows = (
        DailyEstimate.objects.filter(
            estimate_date=day, csp_id__in=list(ids), status__in=("VALIDATED", "PROVISIONAL"),
            estimated_value__isnull=False)
        .values_list("csp_id", "estimated_value", "current_account_count", "status")
    )
    return {c: (v, n, st) for c, v, n, st in rows}


def _estimate_avg_on(day: dt.date, ids) -> tuple[Decimal | None, int]:
    return _weighted_avg((v, n) for v, n, _st in _estimates_by_csp(day, ids).values())


def _flow_state(day: dt.date, coverage: dict, label: str) -> tuple[str, str]:
    """(state, plain-English reason) of the bank-statement data for one day. `label` is how the
    sentence refers to the day ("today" / "yesterday")."""
    e = coverage.get(day)
    if e is None:
        return "no_data", f"there is no bank statement for {label} yet"
    if e["class"] == "COMPLETE_DAY":
        return "complete", f"{label} is complete"
    exp = e.get("latest_export")
    if e["class"] == "PARTIAL_DAY" and exp is not None:
        clock = f"{exp:%I:%M %p}".lstrip("0")
        return "so_far", f"the statement for {label} covers only up to {clock}"
    return "unconfirmed", f"{label} is not yet confirmed complete"


def _change(today, yesterday) -> tuple:
    if today is None or yesterday is None:
        return None, None
    delta = today - yesterday
    pct = (delta / yesterday * 100) if yesterday else None
    return delta, (float(pct) if pct is not None else None)


def _direction(delta) -> str:
    if delta is None:
        return "unknown"
    return "flat" if delta == 0 else ("up" if delta > 0 else "down")


def _latest_refresh_date(ids) -> dt.date | None:
    """The newest day on which SBI's average balance changed for any of these CSPs: the receipt
    date of each CSP's latest value, as stored on its newest estimate row. None if unknown."""
    latest = latest_estimate_date()
    if latest is None or not ids:
        return None
    return DailyEstimate.objects.filter(
        estimate_date=latest, csp_id__in=list(ids), baseline_date__isnull=False,
    ).aggregate(d=Max("baseline_date"))["d"]


def _last_updated() -> dict:
    """When new information last arrived, from the same freshness facts the Data readiness page
    uses. Each value is None when that source has never delivered anything."""
    fr = source_freshness()
    return {
        "sheet_at": fr["calling_sheet"]["last_success"],
        "statements_at": fr["transactions"]["last_processed"],
        "statements_day": fr["transactions"]["last_flow_date"],
    }


def day_compare(today: dt.date | None = None, csp_codes=None) -> dict:
    """The present day against the day before it, for the whole network or for `csp_codes`.

    `today` defaults to the present IST day and `yesterday` is the calendar day before it. Each
    figure is what the stored data says AS OF that day (account counts and the SBI average balance
    are the newest reading on or before the day; money movement is that day's own statement).
    Money movement for today is "so far" until a statement proves the day complete, so a
    today-vs-yesterday money change is marked not like-for-like rather than presented as a trend.
    A missing figure is None, never zero."""
    from . import services

    today = today or timezone.localdate()
    yesterday = today - dt.timedelta(days=1)
    tracked = _tracked()
    ids = [c for c in (csp_codes if csp_codes is not None else tracked) if c in tracked]
    idset = set(ids)
    coverage = services.day_coverage()

    acc = {d: _accounts_as_of(d, ids) for d in (today, yesterday)}
    both = set(acc[today]) & set(acc[yesterday])
    acc_change = sum(acc[today][c] - acc[yesterday][c] for c in both) if both else None
    acc_total = {d: (sum(acc[d].values()) if acc[d] else None) for d in acc}

    new = {d: _new_accounts_as_of(d, ids) for d in (today, yesterday)}
    obs = {d: _observed_avg_as_of(d, ids) for d in (today, yesterday)}
    est = {d: _estimate_avg_on(d, ids) for d in (today, yesterday)}

    fl = {}
    for d in (today, yesterday):
        withdrawn = deposited = o_credit = o_debit = ZERO
        n = o_n = 0
        total = _flow_groups(d, d, idset, by="all").get(None)
        seen = total is not None
        other_cats: dict[str, int] = {}
        if total is not None:
            withdrawn, deposited, n = total.withdrawals, total.deposits, total.n
            o_credit, o_debit, o_n = total.other
            other_cats = {cat: v["count"] for cat, v in cast(dict, total.cats).items()
                          if cat not in COUNTED_CATEGORIES and v["count"]}
        state, note = _flow_state(d, coverage, "today" if d == today else "yesterday")
        fl[d] = {"seen": seen, "withdrawals": withdrawn if seen else None,
                 "deposits": deposited if seen else None, "n": n if seen else None,
                 "other_cats": other_cats,
                 "other_credit": o_credit if seen else None,
                 "other_debit": o_debit if seen else None, "other_n": o_n if seen else None,
                 "state": state, "note": note}
    like_for_like = (fl[today]["state"] == "complete" and fl[yesterday]["state"] == "complete")

    def row(key, label, unit, t, y, *, note="", good_when_up=None, delta=None, pct=None,
            comparable=True):
        d, p = (delta, pct) if delta is not None else _change(t, y)
        return {"key": key, "label": label, "unit": unit, "today": t, "yesterday": y,
                "change": d, "change_pct": p, "direction": _direction(d),
                "good_when_up": good_when_up, "note": note, "like_for_like": comparable}

    acc_note = ""
    if acc_total[today] is not None and acc_total[yesterday] is not None and len(
            acc[today]) != len(acc[yesterday]):
        acc_note = (f"{len(acc[today])} CSPs reporting today and {len(acc[yesterday])} "
                    "yesterday; the change counts only CSPs with both readings.")
    pct_acc = None
    base = acc_total[yesterday]
    if acc_change is not None and base:
        pct_acc = float(Decimal(acc_change) / Decimal(base) * 100)
    n_t, n_y = new[today], new[yesterday]
    month_reset = f"{today:%Y-%m}" != f"{yesterday:%Y-%m}"
    flow_note = ""
    if not like_for_like:
        parts = [str(fl[d]["note"]) for d in (today, yesterday) if fl[d]["state"] != "complete"]
        flow_note = "Not a like-for-like comparison: " + "; ".join(parts) + "."

    rows = [
        row("accounts", "Customer accounts", "count", acc_total[today], acc_total[yesterday],
            good_when_up=True, delta=acc_change, pct=pct_acc, note=acc_note),
        row("new_accounts_mtd", "New accounts this month", "count",
            n_t["mtd"] if n_t else None, n_y["mtd"] if n_y else None, good_when_up=True,
            note=("A new month started: this month's count restarts from zero." if month_reset
                  else "")),
        row("new_accounts_day", "New accounts opened that day", "count",
            n_t["ftd"] if n_t else None, n_y["ftd"] if n_y else None, good_when_up=True,
            note="Today's figure keeps growing until the day ends."),
        row("withdrawals", "Withdrawn by customers", "rupees",
            fl[today]["withdrawals"], fl[yesterday]["withdrawals"], note=flow_note,
            comparable=like_for_like),
        row("deposits", "Deposited by customers", "rupees",
            fl[today]["deposits"], fl[yesterday]["deposits"], good_when_up=True,
            comparable=like_for_like),
        row("transactions", "Transactions counted", "count", fl[today]["n"], fl[yesterday]["n"],
            comparable=like_for_like),
        row("observed_avg", "SBI-reported average balance per account", "rupees",
            obs[today][0], obs[yesterday][0],
            note="Changes only when SBI refreshes a CSP's figure in the Calling Sheet."),
        row("estimated_avg", "Our estimated average balance per account", "rupees",
            est[today][0], est[yesterday][0],
            note="" if est[today][0] is not None or est[yesterday][0] is not None
            else "No reliable estimate yet: the history is still too short."),
    ]
    excluded: dict[str, dict] = {}
    for label, d in (("today", today), ("yesterday", yesterday)):
        for cat, k in cast(dict, fl[d]["other_cats"]).items():
            excluded.setdefault(cat, {"category": cat, "today": 0, "yesterday": 0})[label] = k
    return {
        "today": today, "yesterday": yesterday, "scope": "network" if csp_codes is None else "csp",
        "csp_code": (ids[0] if csp_codes is not None and len(ids) == 1 else None),
        "csps": len(ids), "rows": rows, "like_for_like_money": like_for_like,
        "accounts_reporting": {"today": len(acc[today]), "yesterday": len(acc[yesterday])},
        "excluded": sorted(excluded.values(), key=lambda e: -(e["today"] + e["yesterday"])),
        "sbi_last_sent": _latest_refresh_date(ids),
        "updated": _last_updated(),
        "money_state": {"today": fl[today]["state"], "yesterday": fl[yesterday]["state"],
                        "today_note": fl[today]["note"], "yesterday_note": fl[yesterday]["note"]},
        "other_types": {
            "today": {"credit": fl[today]["other_credit"], "debit": fl[today]["other_debit"],
                      "txn_count": fl[today]["other_n"]},
            "yesterday": {"credit": fl[yesterday]["other_credit"],
                          "debit": fl[yesterday]["other_debit"],
                          "txn_count": fl[yesterday]["other_n"]},
        },
        "notes": ["Today means the present day (India time) and yesterday the day before.",
                  "Figures are what the stored data says as of each day; a missing figure is "
                  "shown as a dash, never as zero."],
    }



# ---------------------------------------------------------------- per-CSP today vs yesterday
DAY_SORTS = {  # each returns the figure to order by, or None when the CSP has no such figure
    "accounts_change": lambda r: r["accounts_change"],
    "new_today": lambda r: r["new_day_today"],
    "withdrawn_today": lambda r: r["withdrawals_today"],
    "deposited_today": lambda r: r["deposits_today"],
    "net_today": lambda r: r["net_today"],
    "net_change": lambda r: r["net_change"],
    "name": lambda r: (r["name"] or r["csp_code"]).lower(),
}


def _diff(a, b):
    return None if a is None or b is None else a - b


def csp_day_compare_rows(
    today: dt.date | None = None, *, search: str = "", sort: str = "accounts_change",
    descending: bool = True, limit: int = 25, offset: int = 0,
) -> tuple[list[dict], int, dict]:
    """Every tracked CSP with the present day against the day before: customer accounts, new
    accounts, customer withdrawals and deposits (the statement's own figures for each day),
    deposits minus withdrawals, the SBI average balance and our estimate. Same definitions as
    `day_compare`, built from a handful of batch queries (not one per CSP) and cached per data
    generation; only search, order and the page are applied per request. A missing figure is
    None; a change needs both days. Returns (page of rows, total rows, day-level meta)."""
    today = today or timezone.localdate()
    rows, meta = _memo(f"bf:csp_day_rows:{today}", lambda: _csp_day_rows(today))
    q = search.strip().lower()
    if q:
        rows = [r for r in rows if q in r["csp_code"].lower() or q in (r["name"] or "").lower()]
    key = DAY_SORTS.get(sort, DAY_SORTS["accounts_change"])
    # a CSP with no such figure goes last whichever way the list is ordered
    present = sorted((r for r in rows if key(r) is not None), key=key, reverse=descending)
    rows = present + [r for r in rows if key(r) is None]
    return rows[offset: offset + limit], len(rows), meta


def _csp_day_rows(today: dt.date) -> tuple[list[dict], dict]:
    from . import services

    yesterday = today - dt.timedelta(days=1)
    tracked = _tracked()
    ids = list(tracked)
    coverage = services.day_coverage()
    d_labels = ((today, "today"), (yesterday, "yesterday"))

    accounts = {d: _accounts_as_of(d, ids) for d, _ in d_labels}
    monthly = {d: {r.csp_id: r for r in _monthly_readings_as_of(d, ids)} for d, _ in d_labels}
    observed = {d: _observed_by_csp(d, ids) for d, _ in d_labels}
    estimates = {d: _estimates_by_csp(d, ids) for d, _ in d_labels}
    flow = {d: _flow_groups(d, d, set(ids), by="csp") for d, _ in d_labels}
    states = {d: _flow_state(d, coverage, label) for d, label in d_labels}
    like_for_like = states[today][0] == "complete" and states[yesterday][0] == "complete"

    rows = []
    for code, csp in tracked.items():
        a_t, a_y = accounts[today].get(code), accounts[yesterday].get(code)
        m_t, m_y = monthly[today].get(code), monthly[yesterday].get(code)
        f_t, f_y = flow[today].get(code), flow[yesterday].get(code)
        o_t, o_y = observed[today].get(code), observed[yesterday].get(code)
        e_t, e_y = estimates[today].get(code), estimates[yesterday].get(code)
        w_t = f_t.withdrawals if f_t else None
        w_y = f_y.withdrawals if f_y else None
        p_t = f_t.deposits if f_t else None
        p_y = f_y.deposits if f_y else None
        net_t, net_y = _diff(p_t, w_t), _diff(p_y, w_y)
        rows.append({
            "csp_code": code, "name": csp.name, "zone": csp.zone,
            "accounts_today": a_t, "accounts_yesterday": a_y, "accounts_change": _diff(a_t, a_y),
            "new_mtd_today": m_t.mtd if m_t else None,
            "new_mtd_yesterday": m_y.mtd if m_y else None,
            "new_day_today": m_t.ftd if m_t else None,
            "new_day_yesterday": m_y.ftd if m_y else None,
            "withdrawals_today": w_t, "withdrawals_yesterday": w_y,
            "withdrawals_change": _diff(w_t, w_y),
            "deposits_today": p_t, "deposits_yesterday": p_y, "deposits_change": _diff(p_t, p_y),
            "net_today": net_t, "net_yesterday": net_y, "net_change": _diff(net_t, net_y),
            "transactions_today": f_t.n if f_t else None,
            "transactions_yesterday": f_y.n if f_y else None,
            "observed_today": o_t[0] if o_t else None,
            "observed_yesterday": o_y[0] if o_y else None,
            "observed_change": _diff(o_t[0] if o_t else None, o_y[0] if o_y else None),
            "estimate_today": e_t[0] if e_t else None,
            "estimate_yesterday": e_y[0] if e_y else None,
            "estimate_change": _diff(e_t[0] if e_t else None, e_y[0] if e_y else None),
            "estimate_status": e_t[2] if e_t else None,
        })
    meta = {
        "today": today, "yesterday": yesterday, "like_for_like_money": like_for_like,
        "money_state": {"today": states[today][0], "yesterday": states[yesterday][0],
                        "today_note": states[today][1], "yesterday_note": states[yesterday][1]},
        "money_note": (
            "" if like_for_like else "Not a like-for-like comparison of money: "
            + "; ".join(states[d][1] for d, _ in d_labels if states[d][0] != "complete") + "."),
        "notes": ["Today means the present day (India time) and yesterday the day before.",
                  "A dash means no figure is stored for that CSP and day, never zero.",
                  "A CSP's change needs both days; with only one day there is no change."],
    }
    return rows, meta


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
    "withdrawals": lambda r: r["withdrawals"] or ZERO,
    "deposits": lambda r: r["deposits"] or ZERO,
    "net_flow": lambda r: r["net_flow"] if r["net_flow"] is not None else Decimal(-(10**15)),
    "estimated_delta": lambda r: (
        r["estimated_delta"] if r["estimated_delta"] is not None else Decimal(-(10**15))
    ),
    "observed_avg": lambda r: r["observed_avg"] or ZERO,
}


def _build_rows(date_from: dt.date, date_to: dt.date) -> tuple[list[dict], dict]:
    """The per-CSP table behind the growth explorer, the alerts and the sidebar badge: computed once
    per data generation. Callers get their own copy (the cache hands back a fresh unpickled one), so
    they can add fields to the rows."""
    return _memo(f"bf:rows:{date_from}:{date_to}", lambda: _compute_rows(date_from, date_to))


def _compute_rows(date_from: dt.date, date_to: dt.date) -> tuple[list[dict], dict]:
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
            "withdrawals": f["withdrawals"] if f else None,
            "deposits": f["deposits"] if f else None,
            # deposits minus withdrawals: positive means customer money grew
            "net_flow": (f["deposits"] - f["withdrawals"]) if f else None,
            "gross_flow": (f["deposits"] + f["withdrawals"]) if f else None,
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
                    f"Customers withdrew more than they deposited: withdrawals exceeded deposits "
                    f"by {ratio:.0%} of this CSP's activity in the range. This is the net cash "
                    "movement through the CSP, not a measured change in balances.",
                    "Review recent activity with the CSP / territory lead before concluding.",
                    rank=float(outflow), net_flow=_f(net), withdrawals=_f(r["withdrawals"]),
                    deposits=_f(r["deposits"]), imbalance_pct=round(ratio * 100, 1))
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
            "reason": f"{insufficient} CSP estimate(s) are shadow estimates not yet validated; "
                      f"the estimator is {est['estimator']['status']}: "
                      f"{est['estimator']['detail']}",
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
