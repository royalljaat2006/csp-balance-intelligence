"""
Freeze today's shadow estimates as immutable predictions.

The daily estimate (bankflow.DailyEstimate) is a working row: provisional days are recomputed until
their statements are complete. A LivePrediction is the opposite: one copy of what was predicted,
with every input that produced it, written once and never changed -- so the estimate can later be
scored against the SBI value that actually followed, whatever happens to the working row.

Freezing reads DailyEstimate and a few source tables; it writes only learning.LivePrediction. It
never touches DailyBalance, DailyEstimate or any Calling Sheet table, and nothing here schedules
itself: it runs when the `freeze_predictions` command is run.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from bankflow import movement
from bankflow.models import AccountCountObservation, DailyEstimate
from django.utils import timezone

from . import registry
from .models import LivePrediction

ZERO = Decimal("0")
SWEEP_DAYS = 14
UNAVAILABLE = DailyEstimate.Status.UNAVAILABLE


def _cycle_flow(
    rows: list[DailyEstimate], base_of: dict[int, dt.date]
) -> dict[tuple[str, dt.date], tuple[Decimal, Decimal]]:
    """Per (CSP, estimate day): eligible (deposits, withdrawals) over that day's cycle so far,
    summed from the cycle's own stored daily rows (the baseline day carries no movement)."""
    if not rows:
        return {}
    wanted = {(r.csp_id, base_of[r.pk]) for r in rows}
    floor = min(base_of[r.pk] for r in rows)
    ceiling = max(r.estimate_date for r in rows)
    cycles: dict[tuple[str, dt.date], list[tuple[dt.date, Decimal, Decimal]]] = defaultdict(list)
    qs = DailyEstimate.objects.filter(
        method=movement.METHOD, estimate_date__gte=floor, estimate_date__lte=ceiling,
        csp_id__in={r.csp_id for r in rows},
    ).values_list("csp_id", "baseline_date", "estimate_date", "eligible_deposits",
                  "eligible_withdrawals")
    for csp, base, day, dep, wd in qs.iterator():
        if base is not None and (csp, base) in wanted and day > base:
            cycles[(csp, base)].append((day, dep or ZERO, wd or ZERO))
    out: dict[tuple[str, dt.date], tuple[Decimal, Decimal]] = {}
    for r in rows:
        days = cycles.get((r.csp_id, base_of[r.pk]), [])
        out[(r.csp_id, r.estimate_date)] = (
            sum((d for day, d, _w in days if day <= r.estimate_date), ZERO),
            sum((w for day, _d, w in days if day <= r.estimate_date), ZERO),
        )
    return out


def _pmjdy(csp_ids: set[str]) -> dict[str, tuple[int, dt.datetime]]:
    out: dict[str, tuple[int, dt.datetime]] = {}
    rows = (AccountCountObservation.objects.filter(csp_id__in=csp_ids, source="pmjdy")
            .order_by("csp_id", "-observed_at").distinct("csp_id")
            .values_list("csp_id", "account_count", "observed_at"))
    for csp, n, at in rows:
        out[csp] = (n, at)
    return out


def _reason(e: DailyEstimate) -> str:
    notes = (e.details or {}).get("provisional_reasons") or []
    text = e.reason_code + (": " + " ".join(notes[:3]) if notes else "")
    return text[:300]


def freeze_predictions(
    estimate_date: dt.date | None = None, *, since_days: int = SWEEP_DAYS,
    include_provisional: bool = False, today: dt.date | None = None,
) -> dict:
    """Freeze the available estimates that are not frozen yet and return the counts.

    By default only FINAL rows are frozen: an estimate whose day's bank statement is proven
    complete, so a prediction is never made from data that was known to be missing. Pass
    `include_provisional` to freeze working rows too; they are labelled provisional and the
    outcome records that. With no `estimate_date`, every day of the last `since_days` is swept.
    A prediction that already exists is left exactly as it was."""
    registry.ensure_registry()
    model = registry.active_model()
    empty = {"created": 0, "already_frozen": 0, "skipped_unavailable": 0, "skipped_provisional": 0}
    if model is None or not model.live_supported:
        return {**empty, "error": "No active method that the live estimator runs."}
    today = today or timezone.localdate()
    qs = DailyEstimate.objects.filter(method=movement.METHOD)
    if estimate_date is not None:
        qs = qs.filter(estimate_date=estimate_date)
    else:
        qs = qs.filter(estimate_date__gte=today - dt.timedelta(days=since_days),
                       estimate_date__lte=today)
    rows = list(qs)
    usable = [e for e in rows if e.status != UNAVAILABLE and e.estimated_value is not None
              and e.baseline_value is not None and e.baseline_date is not None]
    skipped_provisional = 0
    if not include_provisional:
        skipped_provisional = sum(1 for e in usable if not e.is_final)
        usable = [e for e in usable if e.is_final]
    have = set(LivePrediction.objects.filter(model=model, csp_id__in={e.csp_id for e in usable})
               .values_list("csp_id", "estimate_date"))
    fresh = [e for e in usable if (e.csp_id, e.estimate_date) not in have]
    base_of = {e.pk: e.baseline_date for e in fresh if e.baseline_date is not None}
    flow = _cycle_flow(fresh, base_of)
    pmjdy = _pmjdy({e.csp_id for e in fresh})

    objs = []
    for e in fresh:
        deposits, withdrawals = flow[(e.csp_id, e.estimate_date)]
        count, at = pmjdy.get(e.csp_id, (None, None))
        details = e.details or {}
        objs.append(LivePrediction(
            csp_id=e.csp_id, model=model, estimate_date=e.estimate_date,
            period_start=base_of[e.pk], period_end=e.estimate_date,
            baseline_value=e.baseline_value, baseline_first_seen=base_of[e.pk],
            baseline_last_seen=e.last_seen_date,
            baseline_account_count=e.baseline_account_count,
            account_count=e.current_account_count, pmjdy_account_count=count,
            pmjdy_snapshot_at=at,
            flow_deposits=deposits, flow_withdrawals=withdrawals,
            net_customer_movement=deposits - withdrawals, flow_coverage=e.flow_coverage,
            predicted_value=e.estimated_value, predicted_combined=e.estimated_combined,
            carry_forward_value=e.baseline_value, confidence_status=e.status,
            confidence_reason=_reason(e),
            statement_state="complete" if e.is_final else "provisional",
            methodology=model.description,
            inputs={
                "estimate_calculated_at": e.calculated_at.isoformat(),
                "method": e.method, "reason_code": e.reason_code, "is_final": e.is_final,
                "stale": bool(details.get("stale")),
                "count": details.get("count") or {},
                "provisional_reasons": details.get("provisional_reasons") or [],
                "counted_txns": details.get("counted_txns"),
                "hypothesis_run": e.hypothesis_run_id,
                "model": {"name": model.name, "version": model.version,
                          "definition": model.definition},
            },
        ))
    LivePrediction.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)
    return {"created": len(objs), "already_frozen": len(usable) - len(fresh),
            "skipped_unavailable": len(rows) - len([e for e in rows if e.status != UNAVAILABLE
                                                    and e.estimated_value is not None
                                                    and e.baseline_value is not None
                                                    and e.baseline_date is not None]),
            "skipped_provisional": skipped_provisional}
