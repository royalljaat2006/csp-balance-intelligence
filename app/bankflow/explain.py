"""
"Why is this CSP's estimate this value?" -- read-only.

Assembles, for one CSP, what the existing services already stored and computed:

  baseline          DailyEstimate.baseline_value / baseline_date  (SBI Avg Balance, per account)
  account count     DailyEstimate baseline / current count, and where it came from
  combined balance  baseline x account count -- the agreed reading of the Calling Sheet value
  eligible flows    FlowAggregate (withdrawals and deposits only, exact CSP matches only)
  estimate          DailyEstimate.estimated_value with its own method, status and reason

Nothing is estimated here. The movement figures are shown as evidence; they are NOT added to the
estimate, because the stored estimator does not do that (see `not_applied`). The cross-check of
the sheet's own total balance against average x count is a read-only consistency test of the
baseline interpretation, using the raw total the Calling Sheet ingest keeps in `Csp.raw_attrs`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from csp.models import Csp, DailyBalance
from csp.rules import ELIGIBILITY_MIN_ACCOUNTS
from django.utils import timezone

from bankflow import flows, intelligence
from bankflow.models import DailyEstimate, FlowAggregate, HypothesisRun

ZERO = Decimal(0)
STALE_COUNT_DAYS = 3  # same threshold the estimator uses for an old Calling Sheet reading

METHOD_WORDS = {
    "carry_forward": "The last SBI-reported average is carried forward unchanged.",
    "account_count_proportional": "The SBI-reported average is scaled by the change in "
                                  "account count.",
    "growth_only": "The SBI-reported average is scaled by the change in account count.",
    "flow_only": "The SBI-reported average is adjusted by money movement.",
    "growth_and_flow": "The SBI-reported average is adjusted for account growth and money "
                       "movement.",
    "none": "No estimate is produced for this CSP.",
}
REASON_WORDS = {
    "NO_VALIDATED_MODEL": "No estimator has passed validation, so the SBI value is carried "
                          "forward.",
    "COUNT_UNCHANGED_CARRY_FORWARD": "The account count has not changed since the SBI value "
                                     "arrived, so the SBI value is carried forward.",
    "COUNT_RULE_NOT_VALIDATED": "The account count changed, but the rule for using it has not "
                                "passed validation, so the SBI value is carried forward.",
    "BELOW_200_ACCOUNT_GATE": "This CSP has fewer than 200 accounts (the eligibility gate), so "
                              "no estimate is produced.",
    "NO_BASELINE": "No SBI-reported value has been received for this CSP.",
    "MISSING_ACCOUNT_COUNT": "An account count is missing, so the estimate cannot be adjusted.",
    "COUNT_SOURCE_MISMATCH": "The two account counts come from different sources and cannot be "
                             "compared.",
    "INSUFFICIENT_FLOW_COVERAGE": "Bank statements cover too little of the period to use money "
                                  "movement.",
}


def _dec(v) -> Decimal | None:
    try:
        return None if v in (None, "", "None") else Decimal(str(v))
    except InvalidOperation:
        return None


def baseline_check(csp: Csp, observed_avg: Decimal | None) -> dict:
    """Does the Calling Sheet's own total balance equal Avg Balance x account count?

    Avg Balance is shown rounded to the rupee, so a difference of up to half a rupee per account is
    rounding, not a mismatch."""
    total = _dec((csp.raw_attrs or {}).get("sheet_total_balance"))
    n = csp.account_count
    if total is None or observed_avg is None or not n:
        return {"state": "cannot_check", "sheet_total": total, "average_x_count": None,
                "difference": None,
                "text": "The Calling Sheet total balance or account count is not available, so "
                        "the reading of its Avg Balance as a per-account average cannot be "
                        "checked for this CSP."}
    combined = observed_avg * n
    diff = total - combined
    ok = abs(diff) <= Decimal(n) / 2 + Decimal("1")
    return {
        "state": "consistent" if ok else "inconsistent", "sheet_total": total,
        "average_x_count": combined, "difference": diff,
        "text": ("The sheet's total balance matches Avg Balance x account count, as expected "
                 "for a per-account average." if ok else
                 "The sheet's total balance does not match Avg Balance x account count. Do not "
                 "rely on the per-account reading for this CSP until it is checked."),
    }


def explain_estimate(csp_code: str, *, today: dt.date | None = None) -> dict | None:
    csp = Csp.objects.filter(pk=csp_code).first()
    if csp is None:
        return None
    today = today or timezone.localdate()
    est = DailyEstimate.objects.filter(csp=csp).order_by("-estimate_date").first()
    hyp = HypothesisRun.objects.first()
    observed = intelligence.latest_observed([csp_code]).get(csp_code)
    observed_avg = observed["avg"] if observed else None
    check = baseline_check(csp, observed_avg)

    latest_reading = (
        DailyBalance.objects.filter(csp=csp, account_count__isnull=False)
        .exclude(source__in=intelligence.NOT_OBSERVATIONS)
        .order_by("-balance_date").values_list("balance_date", "account_count").first()
    )
    out: dict = {
        "csp": {"code": csp.csp_code, "name": csp.name, "tracked": csp.status not in
                ("historical_only", "invalid_code")},
        "as_of": est.estimate_date if est else None,
        "today": today,
        "check": check,
        "estimator": {"status": hyp.status if hyp else "NOT_RUN",
                      "detail": hyp.detail if hyp else "No evaluation has been run yet.",
                      "selected_method": hyp.selected_method if hyp else "carry_forward"},
        "has_estimate": est is not None,
        "flags": [],
    }
    flags: list[dict] = out["flags"]

    if est is None:
        out["message"] = ("No estimate has been calculated for this CSP yet. The steps below "
                          "appear once the daily estimate has run.")
        return out

    base_n, cur_n = est.baseline_account_count, est.current_account_count
    combined = est.baseline_value * base_n if est.baseline_value is not None and base_n else None
    count_age = (today - latest_reading[0]).days if latest_reading else None
    count_stale = count_age is None or count_age > STALE_COUNT_DAYS

    # movement since the baseline arrived: transactions apply from the NEXT day
    deposits = withdrawals = ZERO
    days: list[dict] = []
    complete = flows.proven_complete_dates()
    movement_available = est.baseline_date is not None
    if movement_available:
        rows = FlowAggregate.objects.filter(
            csp=csp, resolution_mode=flows.MODE_EXACT, flow_date__gt=est.baseline_date,
            flow_date__lte=today,
        ).order_by("flow_date")
        for r in rows:
            counted, _other = intelligence._split_flow(r.by_category)
            deposits += counted["deposits"]
            withdrawals += counted["withdrawals"]
            days.append({
                "date": r.flow_date, "deposits": counted["deposits"],
                "withdrawals": counted["withdrawals"],
                "net": counted["deposits"] - counted["withdrawals"],
                "status": ("incomplete (today)" if r.flow_date >= today
                           else "complete" if r.flow_date in complete
                           else "not yet proven complete"),
            })
    window_days = (today - est.baseline_date).days if est.baseline_date is not None else 0

    out.update({
        "baseline": {
            "per_account_average": est.baseline_value, "received_on": est.baseline_date,
            "last_seen": est.last_seen_date,
            "days_since_received": ((today - est.baseline_date).days
                                    if est.baseline_date else None),
            "source": "SBI Avg Balance, Calling Sheet",
        },
        "account_count": {
            "at_baseline": base_n, "current": cur_n, "change": est.account_count_change,
            "source": est.details.get("count_source"),
            "last_reading": latest_reading[0] if latest_reading else None,
            "reading_age_days": count_age, "stale": count_stale,
            "below_gate": cur_n is not None and cur_n < ELIGIBILITY_MIN_ACCOUNTS,
            "gate": ELIGIBILITY_MIN_ACCOUNTS,
        },
        "combined_baseline": combined,
        "movement": {
            "available": movement_available, "from_exclusive": est.baseline_date,
            "to": today, "deposits": deposits, "withdrawals": withdrawals,
            "net": deposits - withdrawals, "days": days, "window_days": window_days,
            "days_with_statements": len(days),
            "coverage": est.flow_coverage,
            "stored_net": est.flow_net,
            "rule": "Only eligible withdrawals and deposits from exactly matched CSP codes are "
                    "counted. Movement applies from the day after the baseline arrived.",
        },
        "estimate": {
            "per_account_average": est.estimated_value, "delta_vs_baseline": est.estimated_delta,
            "method": est.method, "method_words": METHOD_WORDS.get(est.method, est.method),
            "status": est.status, "reason_code": est.reason_code,
            "reason_words": REASON_WORDS.get(est.reason_code, est.reason_code),
            "expected_error_pct": est.expected_error_pct, "error_basis": est.error_basis,
            "calculated_at": est.calculated_at,
        },
        "not_applied": (
            "Money movement is shown as evidence. The stored estimator does not add it to the "
            "balance: the estimate above comes from the method named there. Applying the agreed "
            "movement rule to the combined balance needs a separate estimator that does not "
            "exist yet."),
    })

    if est.status == "UNAVAILABLE":
        flags.append({"level": "bad", "text": REASON_WORDS.get(est.reason_code,
                                                               "No estimate for this CSP.")})
    if est.status != "VALIDATED":
        flags.append({"level": "warn", "text": "Provisional: this estimate has not been checked "
                      "against SBI refreshes, so treat it as indicative only."})
    if est.method == "carry_forward" and est.status != "UNAVAILABLE":
        flags.append({"level": "info", "text": "Carried forward: the estimate equals the last "
                      "SBI-reported average."})
    if est.details.get("stale"):
        flags.append({"level": "warn", "text": "The SBI value is older than 3 days."})
    if count_stale:
        flags.append({"level": "warn", "text": "The account count reading is stale or missing; "
                      "the last known count is being used."})
    if not cur_n:
        flags.append({"level": "bad", "text": "The account count is missing or zero. It needs "
                      "review."})
    if est.flow_coverage is not None and est.flow_coverage < 1:
        flags.append({"level": "warn", "text": f"Bank statements cover {est.flow_coverage:.0%} "
                      "of the days since the baseline arrived."})
    if check["state"] == "inconsistent":
        flags.append({"level": "bad", "text": check["text"]})
    return out
