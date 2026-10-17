"""
"Why is this CSP's estimate this value?" -- read-only.

Reads what the daily estimator stored for one CSP; nothing is recalculated here:

  baseline          DailyEstimate.baseline_value / baseline_date  (SBI Avg Balance, per account)
  account count     the count at the baseline and the valid count used for the estimate
  combined balance  baseline x account count, then + eligible deposits - eligible withdrawals
  daily movement    one stored row per day of the cycle (eligible deposits, withdrawals, net,
                    combined balance, per-account estimate, final/provisional)
  estimate          DailyEstimate.estimated_value = combined balance / valid account count

The cross-check of the sheet's own total balance against average x count is a read-only
consistency test of the baseline interpretation, using the raw total the Calling Sheet ingest
keeps in `Csp.raw_attrs`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from csp.models import Csp, DailyBalance
from csp.rules import ELIGIBILITY_MIN_ACCOUNTS
from django.utils import timezone

from bankflow import intelligence, movement
from bankflow.models import DailyEstimate, HypothesisRun

ZERO = Decimal(0)

METHOD_WORDS = {
    "movement": "Combined balance = the SBI-reported average x the account count at the baseline, "
                "plus eligible deposits minus eligible withdrawals each day after it. The "
                "per-account figure is that combined balance divided by the valid account count.",
    "carry_forward": "(Retired rule) The last SBI-reported average was carried forward unchanged.",
    "account_count_proportional": "(Retired rule) The SBI-reported average was scaled by the "
                                  "change in account count. This is no longer used.",
    "none": "No estimate is produced for this CSP.",
}
REASON_WORDS = {
    movement.R_BASELINE_DAY: "This is the day the SBI value arrived: the estimate is the SBI "
                             "value alone, and that day's bank movement is not applied again.",
    movement.R_APPLIED: "Eligible deposits and withdrawals from complete bank statements were "
                        "applied to the combined balance.",
    movement.R_NO_MOVEMENT: "The bank statement is complete and shows no eligible movement for "
                            "this CSP, so the combined balance is carried forward.",
    movement.R_GAP: "No bank statement covers this day. The combined balance is carried forward "
                    "and the estimate is provisional; a missing statement is not read as no "
                    "activity.",
    movement.R_PARTIAL: "The day's statement is not proven complete. The movement seen so far "
                        "is applied and the estimate is provisional.",
    movement.R_TODAY: "Today is still in progress. Movement so far is applied and will change "
                      "until the statement is complete.",
    "BELOW_200_ACCOUNT_GATE": "This CSP has fewer than 200 accounts (the eligibility gate), so "
                              "no per-account estimate is produced.",
    "NO_BASELINE": "No SBI-reported value has been received for this CSP.",
    "INVALID_BASELINE": "The SBI-reported value is zero or invalid, so no estimate is produced.",
    "MISSING_ACCOUNT_COUNT": "No valid account count is available (blank or zero), so no "
                             "per-account estimate can be produced.",
    # rows written by the retired rules
    "NO_VALIDATED_MODEL": "(Retired rule) No estimator had passed validation.",
    "COUNT_UNCHANGED_CARRY_FORWARD": "(Retired rule) The count had not changed.",
    "COUNT_RULE_NOT_VALIDATED": "(Retired rule) The count rule had not passed validation.",
}
DAY_STATUS = {True: "Final", False: "Provisional"}


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
    if est.method not in (movement.METHOD, "none"):
        out["has_estimate"] = False
        out["message"] = ("The latest stored estimate for this CSP was produced by a retired "
                          "rule and has no daily breakdown. It is replaced the next time the "
                          "daily estimate runs.")
        return out

    base_n, cur_n = est.baseline_account_count, est.current_account_count
    details = est.details or {}
    cstate = details.get("count") or {}
    count_age = (today - latest_reading[0]).days if latest_reading else None
    combined_baseline = (
        _dec(details.get("baseline_combined"))
        or (est.baseline_value * base_n if est.baseline_value is not None and base_n else None)
    )

    # the stored day-by-day rows of this cycle
    cycle = (
        list(DailyEstimate.objects.filter(
            csp=csp, method=movement.METHOD, baseline_date=est.baseline_date,
            baseline_value=est.baseline_value, estimate_date__lte=est.estimate_date,
        ).order_by("estimate_date"))
        if est.baseline_date is not None else []
    )
    days: list[dict] = []
    deposits = withdrawals = ZERO
    excluded = unclassified = duplicates = counted = 0
    for r in cycle:
        d = r.details or {}
        dep, wd = r.eligible_deposits or ZERO, r.eligible_withdrawals or ZERO
        deposits += dep
        withdrawals += wd
        counted += d.get("counted_txns", 0)
        excluded += d.get("excluded_txns", 0)
        unclassified += d.get("unclassified_txns", 0)
        duplicates += d.get("duplicate_refs", 0)
        days.append({
            "date": r.estimate_date, "deposits": dep, "withdrawals": wd,
            "net": r.net_movement or ZERO, "combined": r.estimated_combined,
            "per_account": r.estimated_value, "count": r.current_account_count,
            "final": r.is_final, "status": DAY_STATUS[r.is_final],
            "note": REASON_WORDS.get(r.reason_code, r.reason_code),
            "provisional_reasons": d.get("provisional_reasons") or [],
        })
    net = deposits - withdrawals
    reconciles = None
    if combined_baseline is not None and est.estimated_combined is not None and cycle:
        reconciles = abs(combined_baseline + net - est.estimated_combined) <= Decimal("0.05") * (
            len(cycle) + 1)
    window_days = len(cycle) - 1 if cycle else 0

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
            "source": details.get("count_source") or "calling_sheet",
            "last_reading": latest_reading[0] if latest_reading else None,
            "reading_age_days": count_age, "stale": bool(cstate.get("stale")),
            "invalid_reading_ignored": bool(cstate.get("invalid_reading_ignored")),
            "valid_since": cstate.get("count_read_on"),
            "below_gate": cur_n is not None and cur_n < ELIGIBILITY_MIN_ACCOUNTS,
            "gate": ELIGIBILITY_MIN_ACCOUNTS,
            "effect": (
                f"The account count changed from {base_n} to {cur_n}. The combined balance is "
                "NOT scaled by this: only the divisor of the per-account figure changes, from "
                "the day the new count was received."
                if base_n and cur_n and base_n != cur_n else
                "The account count is unchanged since the baseline, so the per-account figure "
                "moves only with money movement."),
        },
        "combined_baseline": combined_baseline,
        "combined_now": est.estimated_combined,
        "movement": {
            "available": est.baseline_date is not None, "from_exclusive": est.baseline_date,
            "to": est.estimate_date, "deposits": deposits, "withdrawals": withdrawals,
            "net": net, "days": days[1:], "window_days": window_days,
            "days_with_statements": sum(1 for r in cycle[1:] if (r.details or {}).get(
                "counted_txns")),
            "complete_days": sum(1 for r in cycle[1:] if r.is_final),
            "coverage": est.flow_coverage,
            "counted_txns": counted, "excluded_txns": excluded,
            "unclassified_txns": unclassified, "duplicate_refs": duplicates,
            "reconciles": reconciles,
            "rule": "Only eligible withdrawals and deposits from exactly matched CSP codes are "
                    "counted, from the day after the baseline arrived. A pool debit is a "
                    "customer deposit and raises the balance; a pool credit is a customer "
                    "withdrawal and lowers it.",
        },
        "estimate": {
            "per_account_average": est.estimated_value, "combined": est.estimated_combined,
            "delta_vs_baseline": est.estimated_delta,
            "method": est.method, "method_words": METHOD_WORDS.get(est.method, est.method),
            "status": est.status, "reason_code": est.reason_code,
            "reason_words": REASON_WORDS.get(est.reason_code, est.reason_code),
            "final": est.is_final,
            "provisional_reasons": details.get("provisional_reasons") or [],
            "expected_error_pct": est.expected_error_pct, "error_basis": est.error_basis,
            "calculated_at": est.calculated_at,
        },
    })

    if est.status == "UNAVAILABLE":
        flags.append({"level": "bad", "text": REASON_WORDS.get(est.reason_code,
                                                               "No estimate for this CSP.")})
    if est.status != "VALIDATED":
        flags.append({"level": "warn", "text": "Shadow estimate: it has not been validated "
                      "against SBI refreshes and has not been approved for production use, so "
                      "treat it as indicative only."})
    for reason in details.get("provisional_reasons") or []:
        flags.append({"level": "warn", "text": reason})
    if not cur_n:
        flags.append({"level": "bad", "text": "No valid account count is available. It needs "
                      "review."})
    if duplicates:
        flags.append({"level": "warn", "text": f"{duplicates} transaction(s) in this cycle share "
                      "a reference number with another and are flagged for review. They are "
                      "still counted until reviewed."})
    if check["state"] == "inconsistent":
        flags.append({"level": "bad", "text": check["text"]})
    return out
