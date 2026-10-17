"""
Daily Avg Balance ESTIMATE service -- the movement-based SHADOW estimator.

Writes only to bankflow.DailyEstimate -- never to csp.DailyBalance. Every row separates the
SBI-observed side (baseline value/date/count) from the estimate. The arithmetic lives in
bankflow.movement; this module loads the data, runs it per CSP and stores one row per CSP per day.

Method (see movement.py for the full rule):

  * a cycle starts on the day a new SBI Avg Balance is first seen; that day's estimate is the
    baseline alone (Avg Balance x valid account count = the combined balance);
  * each later day adds that day's eligible deposits and subtracts its eligible withdrawals
    (exactly matched CSP codes only; statement allow-list only);
  * the per-account estimate is the combined balance divided by the day's valid account count.
    An account-count change moves the divisor only -- it never scales the combined balance;
  * days with no movement carry the combined balance forward; days with a missing or incomplete
    statement do too (or apply the part that exists) and are marked provisional.

Stored history: a row is FINAL once its statement data is proven complete and every earlier day of
the cycle was final. Final rows are never rewritten; provisional rows are recomputed from the last
final row on every run until their data is complete. A row written by a retired rule
(carry_forward, account_count_proportional) cannot sit beside movement rows without breaking the
day-by-day chain, so it is replaced by the movement row of the same day; its method, status,
reason and value are kept in that row's details["superseded_row"].

Statuses (the validation verdict of the method, from the latest HypothesisRun)
  VALIDATED              met the pre-registered criteria on held-out SBI refreshes
  PROVISIONAL            ahead of carry-forward but below the sample bar
  INSUFFICIENT_EVIDENCE  not (yet) shown to beat carry-forward; the default
  UNAVAILABLE            no estimate (no baseline, no valid count, or below the 200-account gate)

Nothing here approves the method for production: that is a separate decision. The 200-account
eligibility gate (csp.rules.is_eligible) is applied unchanged.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from decimal import Decimal

import structlog
from common.cache import bumps_generation
from csp.models import Csp
from csp.rules import is_eligible
from django.db import transaction
from django.utils import timezone

from bankflow import flows, movement
from bankflow.account_hypothesis import load_series
from bankflow.models import DailyEstimate, FlowAggregate, HypothesisRun, UnresolvedFlowDaily

logger = structlog.get_logger("bankflow")

S = DailyEstimate.Status
ZERO = Decimal("0")
CENT = Decimal("0.01")
STALE_AFTER_DAYS = 3
METHOD = movement.METHOD

_UPDATE_FIELDS = [
    "method", "status", "reason_code", "baseline_value", "baseline_date",
    "last_seen_date", "estimated_value", "estimated_delta",
    "baseline_account_count", "current_account_count", "account_count_change",
    "flow_net", "flow_coverage", "expected_error_pct", "error_basis",
    "estimated_combined", "net_movement", "eligible_deposits", "eligible_withdrawals",
    "is_final", "hypothesis_run", "details", "calculated_at",
]


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(CENT)


def _baseline(rows: list[tuple[dt.date, Decimal, int | None]]):
    """(first_seen, value, count_at_first_seen, last_seen, latest_count) of the
    latest value run, or None."""
    rows = sorted(rows, key=lambda r: r[0])
    if not rows:
        return None
    value = rows[-1][1]
    first = rows[-1]
    for r in reversed(rows):
        if r[1] != value:
            break
        first = r
    return first[0], value, first[2], rows[-1][0], rows[-1][2]


def _expected_error(hyp, method: str) -> float | None:
    """Measured p90 absolute % error of the method on held-out data, or None."""
    try:
        return hyp.metrics["candidates"][method].get("p90_ape_pct")
    except (KeyError, TypeError, AttributeError):
        return None


def method_status(hyp: HypothesisRun | None) -> str:
    """The validation verdict that labels every movement estimate."""
    if hyp is not None and hyp.selected_method == METHOD and hyp.status in (
        S.VALIDATED, S.PROVISIONAL
    ):
        return hyp.status
    return S.INSUFFICIENT


def _row(code, estimate_date, now, status, reason, method, **extra) -> dict:
    return {
        "csp_id": code, "estimate_date": estimate_date, "status": status,
        "reason_code": reason, "method": method, "calculated_at": now, "details": {},
        **extra,
    }


def _movements(first_exclusive: dt.date, end: dt.date, codes: set[str]):
    """({csp: {day: DayMovement}}, days with any statement rows) for exactly matched codes."""
    per: dict[str, dict[dt.date, movement.DayMovement]] = defaultdict(dict)
    days: set[dt.date] = set()
    qs = FlowAggregate.objects.filter(
        resolution_mode=flows.MODE_EXACT, flow_date__gt=first_exclusive, flow_date__lte=end
    ).values_list("csp_id", "flow_date", "by_category", "duplicate_ref_count",
                  "reversal_candidate_amount")
    for csp, day, cats, dups, rev in qs.iterator():
        days.add(day)
        if csp in codes:
            per[csp][day] = movement.split(cats, duplicate_refs=dups, reversal_amount=rev)
    days.update(UnresolvedFlowDaily.objects.filter(
        flow_date__gt=first_exclusive, flow_date__lte=end).values_list("flow_date", flat=True))
    return per, days


def _jsonable_state(st: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in st.items()}


@bumps_generation
def compute_daily_estimates(estimate_date: dt.date | None = None) -> dict:
    estimate_date = estimate_date or timezone.localdate()
    today = timezone.localdate()
    now = timezone.now()
    hyp = HypothesisRun.objects.first()
    hyp_status = hyp.status if hyp else "NOT_RUN"
    verdict = method_status(hyp)
    series = load_series(until=estimate_date)
    csps = {c.csp_code: c for c in Csp.objects.tracked()}
    complete = flows.proven_complete_dates()
    complete_sorted = sorted(complete)

    finals = {
        e.csp_id: e
        for e in DailyEstimate.objects.filter(
            method=METHOD, is_final=True, estimate_date__lte=estimate_date,
            estimated_combined__isnull=False, csp_id__in=list(csps),
        ).order_by("csp_id", "-estimate_date").distinct("csp_id")
    }

    built: dict[str, dict] = {}
    plans: dict[str, dict] = {}
    for code in csps:
        rows = series.get(code, [])
        base = _baseline(rows)
        if base is None:
            built[code] = _row(code, estimate_date, now, S.UNAVAILABLE, "NO_BASELINE", "none")
            continue
        first, value, _n0, last_seen, _n1 = base
        if value <= 0:
            built[code] = _row(code, estimate_date, now, S.UNAVAILABLE, "INVALID_BASELINE",
                               "none", baseline_value=value, baseline_date=first,
                               last_seen_date=last_seen)
            continue
        counts = movement.CountTimeline([(d, n) for d, _a, n in rows])
        start_count, _seen = counts.valid_on(first)
        if start_count is None:
            built[code] = _row(code, estimate_date, now, S.UNAVAILABLE, "MISSING_ACCOUNT_COUNT",
                               "none", baseline_value=value, baseline_date=first,
                               last_seen_date=last_seen)
            continue
        fin = finals.get(code)
        resume = (
            fin is not None and fin.baseline_date == first and fin.baseline_value == value
            and fin.estimate_date >= first
        )
        if resume and fin is not None and fin.estimated_combined is not None:
            prev = movement.Day(
                fin.estimate_date, fin.estimated_combined, fin.current_account_count, {},
                movement.DayMovement(), False, True, fin.reason_code,
            )
            cum0 = fin.flow_net or ZERO
        else:
            resume = False
            prev = movement.baseline_day(first, value, start_count, counts)
            cum0 = ZERO
        plans[code] = {"first": first, "value": value, "start_count": start_count,
                       "last_seen": last_seen, "counts": counts, "prev": prev, "cum0": cum0,
                       "resumed": resume}

    flow_by_csp: dict[str, dict] = {}
    statement_days: set[dt.date] = set()
    legacy: dict[tuple[str, dt.date], dict] = {}
    if plans:
        floor = min(p["prev"].date for p in plans.values())
        flow_by_csp, statement_days = _movements(floor, estimate_date, set(plans))
        for old in DailyEstimate.objects.filter(
            estimate_date__gte=min(p["first"] for p in plans.values()),
            estimate_date__lte=estimate_date,
        ).exclude(method=METHOD):
            legacy[(old.csp_id, old.estimate_date)] = {
                "method": old.method, "status": old.status, "reason_code": old.reason_code,
                "estimated_value": None if old.estimated_value is None
                else str(old.estimated_value),
                "calculated_at": old.calculated_at.isoformat(),
            }

    n_provisional = 0
    for code, p in plans.items():
        first, value, start_count = p["first"], p["value"], p["start_count"]
        days = [] if p["resumed"] else [p["prev"]]
        days += movement.roll(
            previous=p["prev"], end=estimate_date, movements=flow_by_csp.get(code, {}),
            complete_days=complete, statement_days=statement_days, counts=p["counts"],
            today=today,
        )
        cum = p["cum0"]
        for d in days:
            cum += d.movement.net
            age = max(0, (d.date - p["last_seen"]).days)
            reasons = list(d.provisional)
            if age > STALE_AFTER_DAYS:
                reasons.append("The SBI value has not been refreshed for more than 3 days.")
            if d.count_state.get("stale"):
                reasons.append("The account count reading is stale or was blank/zero; the last "
                               "valid count is in use.")
            span = (d.date - first).days
            cov = None
            if span:
                done = (bisect.bisect_right(complete_sorted, d.date)
                        - bisect.bisect_right(complete_sorted, first))
                cov = round(done / span, 4)
            row = _row(
                code, d.date, now, verdict, d.reason, METHOD,
                baseline_value=value, baseline_date=first, last_seen_date=p["last_seen"],
                baseline_account_count=start_count, current_account_count=d.count,
                account_count_change=(d.count - start_count) if d.count else None,
                estimated_combined=_q(d.combined), net_movement=_q(d.movement.net),
                eligible_deposits=_q(d.movement.deposits),
                eligible_withdrawals=_q(d.movement.withdrawals),
                flow_net=_q(cum), flow_coverage=cov, is_final=d.final, hypothesis_run=hyp,
            )
            row["details"].update(
                cycle_start=first.isoformat(), baseline_combined=str(_q(value * start_count)),
                count_source="calling_sheet", count=_jsonable_state(d.count_state),
                days_since_last_seen=age, stale=age > STALE_AFTER_DAYS,
                provisional_reasons=reasons, provisional=bool(reasons) or not d.final,
                movement_applied=d.applied, counted_txns=d.movement.counted_txns,
                excluded_txns=d.movement.excluded_txns,
                unclassified_txns=d.movement.unclassified_txns,
                duplicate_refs=d.movement.duplicate_refs,
                reversal_candidate_amount=str(_q(d.movement.reversal_amount)),
            )
            if (code, d.date) in legacy:
                row["details"]["superseded_row"] = legacy[(code, d.date)]
            if d.count is None:
                row.update(status=S.UNAVAILABLE, reason_code="MISSING_ACCOUNT_COUNT")
            elif not is_eligible(d.count):
                row.update(status=S.UNAVAILABLE, reason_code="BELOW_200_ACCOUNT_GATE")
            else:
                per_account = d.per_account
                row["estimated_value"] = per_account
                row["estimated_delta"] = (per_account - value) if per_account is not None else None
                if verdict == S.VALIDATED and hyp is not None:
                    p90 = _expected_error(hyp, METHOD)
                    if p90 is not None:
                        row["expected_error_pct"] = p90
                        row["error_basis"] = (
                            f"90th-percentile abs % error, held-out, {METHOD} "
                            f"(run #{hyp.pk}, {hyp.n_pairs} pairs / {hyp.n_events} events)"
                        )
            if reasons and d.date == estimate_date:
                n_provisional += 1
            built[f"{code}|{d.date}"] = row

    objs = [
        DailyEstimate(**{k: v for k, v in r.items() if k != "hypothesis_run"},
                      hypothesis_run=r.get("hypothesis_run"))
        for r in built.values()
    ]
    with transaction.atomic():
        DailyEstimate.objects.bulk_create(
            objs, update_conflicts=True, unique_fields=["csp", "estimate_date"],
            update_fields=_UPDATE_FIELDS, batch_size=2000,
        )
    by_status: dict[str, int] = {}
    for r in built.values():
        if r["estimate_date"] == estimate_date:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    logger.info("daily_estimates", stage="ESTIMATE", date=str(estimate_date),
                counts=by_status, provisional=n_provisional)
    return {"estimate_date": estimate_date, "rows": len(objs), "by_status": by_status,
            "provisional": n_provisional, "hypothesis_status": hyp_status,
            "selected_method": METHOD, "validation_status": verdict}


def estimate_rows(
    *, estimate_date: dt.date | None = None, status: str | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[list[dict], int, dt.date | None]:
    if estimate_date is None:
        latest = DailyEstimate.objects.order_by("-estimate_date").values_list(
            "estimate_date", flat=True).first()
        estimate_date = latest
    if estimate_date is None:
        return [], 0, None
    qs = DailyEstimate.objects.filter(estimate_date=estimate_date).select_related("csp")
    if status:
        qs = qs.filter(status=status)
    total = qs.count()
    out = []
    for e in qs.order_by("csp_id")[offset: offset + limit]:
        out.append({
            "csp_code": e.csp_id,
            "csp_name": e.csp.name,
            "estimate_date": e.estimate_date,
            "observed": {  # SBI-provided, from the Calling Sheet
                "avg_balance": e.baseline_value,
                "first_seen_date": e.baseline_date,
                "last_seen_date": e.last_seen_date,
                "account_count_at_observation": e.baseline_account_count,
            },
            "estimate": {  # model output -- NOT an actual balance
                "avg_balance": e.estimated_value,
                "combined_balance": e.estimated_combined,
                "delta_vs_observed": e.estimated_delta,
                "method": e.method,
                "current_account_count": e.current_account_count,
                "account_count_change": e.account_count_change,
            },
            "movement": {
                "eligible_deposits": e.eligible_deposits,
                "eligible_withdrawals": e.eligible_withdrawals,
                "net_eligible": e.net_movement,
            },
            "flow": {"net_attributed": e.flow_net, "coverage": e.flow_coverage},
            "final": e.is_final,
            "status": e.status,
            "reason_code": e.reason_code,
            "expected_error_pct": e.expected_error_pct,
            "error_basis": e.error_basis,
            "details": e.details,
            "calculated_at": e.calculated_at,
        })
    return out, total, estimate_date


def estimate_overview() -> dict:
    """Status counts for the latest estimate date plus the hypothesis verdict."""
    latest = DailyEstimate.objects.order_by("-estimate_date").values_list(
        "estimate_date", flat=True).first()
    counts: dict[str, int] = {}
    last_calc = None
    if latest:
        for s, in DailyEstimate.objects.filter(estimate_date=latest).values_list("status"):
            counts[s] = counts.get(s, 0) + 1
        last_calc = DailyEstimate.objects.filter(estimate_date=latest).order_by(
            "-calculated_at").values_list("calculated_at", flat=True).first()
    return {"estimate_date": latest, "by_status": counts, "last_calculated_at": last_calc,
            "hypothesis": hypothesis_overview(),
            "note": ("Estimates are model output stored apart from the SBI-provided Avg Balance. "
                     "Only VALIDATED carries an error figure.")}


def hypothesis_overview() -> dict:
    run = HypothesisRun.objects.first()
    if run is None:
        return {"status": "NOT_RUN", "detail": "The account-count hypothesis has not been tested."}
    return {"status": run.status, "detail": run.detail, "selected_method": run.selected_method,
            "parameters": run.parameters, "n_pairs": run.n_pairs, "n_events": run.n_events,
            "metrics": run.metrics, "config": run.config, "created_at": run.created_at}
