"""
Daily Avg Balance ESTIMATE service.

Writes only to bankflow.DailyEstimate -- never to csp.DailyBalance. Every row
separates the SBI-observed side (baseline value/date/count) from the estimate.

Method, in full (nothing else feeds `estimated_value`):

  * default `carry_forward`: estimate = last observed value. This is the
    no-change baseline every other rule must beat; it is a placeholder, not a
    validated model, so its status is INSUFFICIENT_EVIDENCE.
  * `account_count_proportional`: estimate = baseline * current_count /
    baseline_count, used ONLY when the latest account-count HypothesisRun is
    VALIDATED or PROVISIONAL and the count actually changed. The row inherits
    that run's status. Expected error is filled in only for VALIDATED, from that
    run's measured 90th-percentile absolute % error.
  * attributed pool flow (exact KO matches, proven-complete statement days only)
    is reported next to the estimate with its coverage. It is NOT in the value:
    no flow model has passed validation (see evaluate_bank_flows).

Statuses
  VALIDATED              count rule met the pre-registered criteria on held data
  PROVISIONAL            count rule ahead of carry-forward but below the sample bar
  INSUFFICIENT_EVIDENCE  estimate is the unvalidated carry-forward
  UNAVAILABLE            no estimate (no baseline, or below the 200-account gate)

The 200-account eligibility gate (csp.rules.is_eligible) is applied unchanged.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import structlog
from csp.models import Csp
from csp.rules import is_eligible
from django.db import transaction
from django.utils import timezone

from bankflow import flows, pmjdy
from bankflow.account_hypothesis import PRIMARY, load_series, predict
from bankflow.model_comparison import MIN_COVERAGE
from bankflow.models import DailyEstimate, HypothesisRun

logger = structlog.get_logger("bankflow")

S = DailyEstimate.Status
ZERO = Decimal("0")
CENT = Decimal("0.01")
STALE_AFTER_DAYS = 3


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
    """Measured p90 absolute % error of the selected method on held-out data, or None."""
    try:
        if method == "growth_only":
            return hyp.metrics["candidates"][PRIMARY].get("p90_ape_pct")
        m = hyp.metrics["comparison"]["methods"][method]["metrics"]
        return m["candidates"][method].get("p90_ape_pct")
    except (KeyError, TypeError):
        return None


def compute_daily_estimates(estimate_date: dt.date | None = None) -> dict:
    estimate_date = estimate_date or timezone.localdate()
    now = timezone.now()
    hyp = HypothesisRun.objects.first()
    hyp_status = hyp.status if hyp else "NOT_RUN"
    selected = hyp.selected_method if hyp else "carry_forward"
    usable_model = (
        bool(hyp)
        and hyp_status in (S.VALIDATED, S.PROVISIONAL)
        and selected != "carry_forward"
    )
    series = load_series(until=estimate_date)
    csps = {c.csp_code: c for c in Csp.objects.tracked()}
    pmjdy_now = pmjdy.latest_counts(csps)

    built: dict[str, dict] = {}
    pmjdy_base: dict[dt.date, dict] = {}
    for code, csp in csps.items():
        base = _baseline(series.get(code, []))
        if base is None:
            built[code] = _row(code, estimate_date, now, S.UNAVAILABLE, "NO_BASELINE", "none")
            continue
        first, value, sheet_base_n, last_seen, sheet_latest_n = base
        if first not in pmjdy_base:
            pmjdy_base[first] = pmjdy.counts_as_of(first, [c for c in csps])
        p_now = pmjdy_now.get(code)
        p_base = pmjdy_base[first].get(code)
        if p_now is not None and p_base is not None:
            cur_n, base_n, src = p_now[0], p_base[0], "pmjdy"
        elif p_now is None:
            cur_n, base_n, src = sheet_latest_n, sheet_base_n, "calling_sheet"
            if cur_n is None:
                cur_n = csp.account_count
        else:  # PMJDY now, but no PMJDY reading at the baseline date: counts are not comparable
            cur_n, base_n, src = p_now[0], sheet_base_n, "mixed"
        row = _row(
            code, estimate_date, now, S.INSUFFICIENT, "NO_VALIDATED_MODEL", "carry_forward",
            baseline_value=value, baseline_date=first, last_seen_date=last_seen,
            baseline_account_count=base_n, current_account_count=cur_n,
            account_count_change=(
                cur_n - base_n if cur_n is not None and base_n is not None else None
            ),
            hypothesis_run=hyp,
        )
        row["details"].update(
            days_since_last_seen=(estimate_date - last_seen).days,
            stale=(estimate_date - last_seen).days > STALE_AFTER_DAYS,
            count_source=src,
            current_count_observed_at=p_now[1].isoformat() if p_now else None,
        )
        if not is_eligible(cur_n):
            row.update(status=S.UNAVAILABLE, reason_code="BELOW_200_ACCOUNT_GATE", method="none")
        else:
            row["estimated_value"] = _q(value)
            row["estimated_delta"] = ZERO
        built[code] = row

    _attach_flows(built, estimate_date)

    for row in built.values():
        if row["status"] == S.UNAVAILABLE or row.get("baseline_value") is None:
            continue
        value = row["baseline_value"]
        base_n, cur_n = row["baseline_account_count"], row["current_account_count"]
        change = row["account_count_change"]
        if base_n is None or cur_n is None:
            row["reason_code"] = "MISSING_ACCOUNT_COUNT"
            continue
        if row["details"]["count_source"] == "mixed":
            row["reason_code"] = "COUNT_SOURCE_MISMATCH"
            row["details"]["note"] = (
                "PMJDY count now vs Calling Sheet count at baseline: not comparable."
            )
            continue
        growth_value = predict(PRIMARY, value, base_n, cur_n).quantize(CENT)
        flow_x = None
        cov = row.get("flow_coverage")
        if row.get("flow_net") is not None and cur_n:
            flow_x = float(row["flow_net"]) / cur_n
        candidate, reason = None, None
        if usable_model and selected == "growth_only" and change != 0:
            candidate = growth_value
        elif usable_model and selected in ("flow_only", "growth_and_flow"):
            beta = ((hyp.parameters if hyp else None) or {}).get("beta")
            if beta is None or flow_x is None or (cov or 0) < MIN_COVERAGE:
                reason = "INSUFFICIENT_FLOW_COVERAGE"
            else:
                base_part = growth_value if selected == "growth_and_flow" else value
                candidate = _q(base_part + Decimal(str(beta * flow_x)))
        if candidate is not None:
            row.update(
                method=selected, status=hyp_status, reason_code=f"{selected.upper()}_{hyp_status}",
                estimated_value=candidate, estimated_delta=candidate - value,
            )
            if hyp_status == S.VALIDATED and hyp is not None:
                p90 = _expected_error(hyp, selected)
                if p90 is not None:
                    row["expected_error_pct"] = p90
                    row["error_basis"] = (
                        f"90th-percentile abs % error, held-out, {selected} "
                        f"(run #{hyp.pk}, {hyp.n_pairs} pairs / {hyp.n_events} events)"
                    )
        elif reason:
            row["reason_code"] = reason
        elif change == 0:
            row["reason_code"] = "COUNT_UNCHANGED_CARRY_FORWARD"
        else:
            row["reason_code"] = "COUNT_RULE_NOT_VALIDATED"
            row["details"]["unvalidated_count_scaled_value"] = str(growth_value)
            row["details"]["unvalidated_note"] = (
                f"Shown for information only; estimator status is {hyp_status}."
            )

    objs = [
        DailyEstimate(**{k: v for k, v in r.items() if k != "hypothesis_run"},
                      hypothesis_run=r.get("hypothesis_run"))
        for r in built.values()
    ]
    with transaction.atomic():
        DailyEstimate.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["csp", "estimate_date"],
            update_fields=[
                "method", "status", "reason_code", "baseline_value", "baseline_date",
                "last_seen_date", "estimated_value", "estimated_delta",
                "baseline_account_count", "current_account_count", "account_count_change",
                "flow_net", "flow_coverage", "expected_error_pct", "error_basis",
                "hypothesis_run", "details", "calculated_at",
            ],
        )
    counts: dict[str, int] = {}
    for r in built.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    logger.info("daily_estimates", stage="ESTIMATE", date=str(estimate_date), counts=counts)
    return {"estimate_date": estimate_date, "rows": len(objs), "by_status": counts,
            "hypothesis_status": hyp_status, "selected_method": selected}


def _row(code, estimate_date, now, status, reason, method, **extra) -> dict:
    return {
        "csp_id": code, "estimate_date": estimate_date, "status": status,
        "reason_code": reason, "method": method, "calculated_at": now, "details": {},
        **extra,
    }


def _attach_flows(built: dict[str, dict], estimate_date: dt.date) -> None:
    """Attributed (exact-only) signed pool flow since each CSP's baseline date."""
    complete = flows.proven_complete_dates()
    by_start: dict[dt.date, list[str]] = {}
    for code, r in built.items():
        if r.get("baseline_date") and r["status"] != S.UNAVAILABLE:
            by_start.setdefault(r["baseline_date"], []).append(code)
    for start, codes in by_start.items():
        per_csp, coverage = flows.window_flows(
            codes, start, estimate_date, definition=flows.DEFAULT_DEFINITION,
            mode=flows.MODE_EXACT, complete_only=True, complete_days=complete,
        )
        for code in codes:
            built[code]["flow_coverage"] = round(coverage, 4)
            built[code]["flow_net"] = _q(per_csp.get(code, ZERO)) if coverage > 0 else None


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
                "delta_vs_observed": e.estimated_delta,
                "method": e.method,
                "current_account_count": e.current_account_count,
                "account_count_change": e.account_count_change,
            },
            "flow": {"net_attributed": e.flow_net, "coverage": e.flow_coverage},
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
