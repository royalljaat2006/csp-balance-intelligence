"""
Historical reconciliation of the SHADOW estimates: drift detection and authorised correction.

Why this exists. A day's estimate row becomes FINAL once its statement data is proven complete and
every earlier day of the cycle was final; the daily run never rewrites a final row. Provisional days
(missing or partial statements) already refill themselves when the statement arrives. What the daily
run cannot do, by design, is change a final day. If statement data for a final day later differs
(a corrected re-export, a late row), the stored estimate is silently based on old evidence.

What this module does about it, and nothing more:

  * `estimate_drift()` is READ ONLY. It compares each final row's stored eligible deposits and
    withdrawals with the statement data now and reports every day that differs. It changes nothing.
    The Review page shows the result, so ordinary late data is visible, not silent.

  * `apply_correction()` is the only way to change a final row, and it is explicit:
      - the actor must be an active superuser (the repository has no narrower approval role);
      - a written reason is required;
      - only that CSP's current cycle is recomputed, from the chosen day forward, with the
        agreed movement rule (bankflow.estimates.build_estimates);
      - if nothing would change, nothing is written (idempotent) and the attempt is still logged;
      - otherwise the superseded rows are snapshotted into an insert-only EstimateCorrection record
        (actor, reason, time, days, old values) and each rewritten row carries `details.correction`.
    Month-to-date averages and the month-end outlook read the stored rows, so they follow.

It never touches csp.DailyBalance, csp.MonthlySummary, Rule #19 or any production figure. There is
deliberately no web or API entry point: exposing a correction to the UI needs an approval mechanism
the repository does not have. It runs from `manage.py correct_estimate` on the server.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from common.cache import bumps_generation
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from bankflow import estimates, flows, movement
from bankflow.models import DailyEstimate, EstimateCorrection, FlowAggregate

ZERO = Decimal("0")
CENT = Decimal("0.01")
MIN_REASON_CHARS = 10
DRIFT_LOOKBACK_DAYS = 62
_COMPARED = ("estimated_combined", "net_movement", "eligible_deposits", "eligible_withdrawals",
             "is_final", "reason_code")


class CorrectionError(ValueError):
    """The request is not a valid correction (nothing was changed)."""


def is_authorised(user) -> bool:
    """Who may correct final estimates: an active superuser. Staff status alone is not enough."""
    return bool(user is not None and getattr(user, "is_active", False)
                and getattr(user, "is_superuser", False))


def _q(v) -> Decimal:
    return (v if v is not None else ZERO).quantize(CENT)


def estimate_drift(
    csp_code: str | None = None, *, today: dt.date | None = None,
    lookback_days: int = DRIFT_LOOKBACK_DAYS,
) -> list[dict]:
    """Final movement rows whose stored eligible deposits/withdrawals differ from the statement
    data now. Read only. The baseline day applies no movement and is skipped."""
    today = today or timezone.localdate()
    since = today - dt.timedelta(days=lookback_days)
    qs = DailyEstimate.objects.filter(
        method=movement.METHOD, is_final=True, estimate_date__gte=since,
    ).exclude(reason_code=movement.R_BASELINE_DAY)
    if csp_code:
        qs = qs.filter(csp_id=csp_code)
    rows = list(qs.order_by("csp_id", "estimate_date"))
    if not rows:
        return []
    now: dict[tuple[str, dt.date], movement.DayMovement] = {}
    for csp, day, cats in FlowAggregate.objects.filter(
        resolution_mode=flows.MODE_EXACT, csp_id__in={r.csp_id for r in rows},
        flow_date__gte=min(r.estimate_date for r in rows),
        flow_date__lte=max(r.estimate_date for r in rows),
    ).values_list("csp_id", "flow_date", "by_category").iterator():
        now[(csp, day)] = movement.split(cats)
    out = []
    for r in rows:
        cur = now.get((r.csp_id, r.estimate_date), movement.DayMovement())
        dep, wd = _q(cur.deposits), _q(cur.withdrawals)
        if dep != _q(r.eligible_deposits) or wd != _q(r.eligible_withdrawals):
            out.append({
                "csp_code": r.csp_id, "date": r.estimate_date,
                "stored_deposits": _q(r.eligible_deposits), "stored_withdrawals": _q(
                    r.eligible_withdrawals),
                "current_deposits": dep, "current_withdrawals": wd,
                "net_difference": (dep - wd) - _q(r.net_movement),
            })
    return out


def _snapshot(e: DailyEstimate) -> dict:
    def s(v):
        return None if v is None else str(v)

    return {
        "date": e.estimate_date.isoformat(), "method": e.method, "status": e.status,
        "reason_code": e.reason_code, "estimated_combined": s(e.estimated_combined),
        "estimated_value": s(e.estimated_value), "net_movement": s(e.net_movement),
        "eligible_deposits": s(e.eligible_deposits),
        "eligible_withdrawals": s(e.eligible_withdrawals), "is_final": e.is_final,
        "calculated_at": e.calculated_at.isoformat(),
        "earlier_correction": (e.details or {}).get("correction"),
    }


def _same(old: DailyEstimate, new: dict) -> bool:
    return all(getattr(old, f) == new.get(f) for f in _COMPARED)


@bumps_generation
def apply_correction(
    csp_code: str, from_date: dt.date, *, reason: str, actor, estimate_date: dt.date | None = None,
) -> EstimateCorrection:
    """Recompute one CSP's current cycle from `from_date` and record why. See the module docstring.

    Raises PermissionDenied (actor not an active superuser) or CorrectionError (invalid request);
    in both cases nothing is written."""
    if not is_authorised(actor):
        raise PermissionDenied("Only an active superuser may correct final estimates.")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise CorrectionError(f"A reason of at least {MIN_REASON_CHARS} characters is required.")
    latest = DailyEstimate.objects.filter(
        csp_id=csp_code, method=movement.METHOD,
        **({"estimate_date__lte": estimate_date} if estimate_date else {}),
    ).order_by("-estimate_date").first()
    if latest is None or latest.baseline_date is None:
        raise CorrectionError("This CSP has no movement estimate to correct.")
    end = latest.estimate_date
    if from_date <= latest.baseline_date:
        raise CorrectionError("Movement starts the day after the baseline arrived; choose a day "
                              f"after {latest.baseline_date}.")
    if from_date > end:
        raise CorrectionError(f"{from_date} is after the latest estimate ({end}).")
    cycle = DailyEstimate.objects.filter(
        csp_id=csp_code, method=movement.METHOD, baseline_date=latest.baseline_date,
        baseline_value=latest.baseline_value,
    )
    if not cycle.filter(estimate_date__lt=from_date).exists():
        raise CorrectionError("No estimate exists before that day in the current cycle.")
    existing = {e.estimate_date: e for e in cycle.filter(
        estimate_date__gte=from_date, estimate_date__lte=end)}

    with transaction.atomic():
        built, meta = estimates.build_estimates(end, only_csp=csp_code, restart_from=from_date)
        new = {r["estimate_date"]: r for r in built.values()
               if r["csp_id"] == csp_code and r["estimate_date"] >= from_date}
        changed = sorted(d for d, r in new.items()
                         if d not in existing or not _same(existing[d], r))
        base = {"csp_id": csp_code, "baseline_date": latest.baseline_date, "from_date": from_date,
                "to_date": end, "reason": reason, "actor": actor.get_username()}
        if not changed:
            return EstimateCorrection.objects.create(
                **base, outcome=EstimateCorrection.Outcome.NO_CHANGE,
                summary={"note": "Recomputing from the current data gave the same stored rows."})
        first = changed[0]
        record = EstimateCorrection.objects.create(
            **base, outcome=EstimateCorrection.Outcome.APPLIED,
            affected_dates=[d.isoformat() for d in sorted(new) if d >= first],
            superseded=[_snapshot(existing[d]) for d in sorted(existing) if d >= first],
            summary={"combined_before": str(_q(existing[end].estimated_combined))
                     if end in existing else None,
                     "combined_after": str(_q(new[end]["estimated_combined"])) if end in new
                     else None, "first_changed_day": first.isoformat()})
        mark = {"correction": {"id": record.pk, "actor": record.actor,
                               "at": timezone.now().isoformat(), "reason": reason}}
        rewrite = {f"{csp_code}|{d}": {**new[d], "details": {**new[d]["details"], **mark}}
                   for d in sorted(new) if d >= first}
        estimates.write_rows(rewrite, end, meta)
        return record


def group_drift(items: list[dict]) -> dict[str, dict]:
    """Per CSP: the first drifted day (where a correction should start) and the days affected."""
    out: dict[str, dict] = defaultdict(lambda: {"days": [], "first": None})
    for i in items:
        g = out[i["csp_code"]]
        g["days"].append(i["date"])
        g["first"] = min(g["first"], i["date"]) if g["first"] else i["date"]
    return dict(out)
