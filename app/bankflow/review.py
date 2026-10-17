"""
Review workspace -- everything that needs a person's look, in one read-only roll-up.

Nothing here changes data or a calculation. A category being "clear" or "open" is a statement
about the stored data right now; clearing it is not an action this module (or the page) offers.
Statement rows that cannot be linked to a CSP, carry an unapproved label, or look like duplicates or
reversals are already kept out of (or flagged within) every estimate by the ingest; this module only
makes that visible with counts, amounts and the next step.

Late rows are flagged, never used to trigger a recalculation of earlier days.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from csp.models import Csp, DailyBalance, IngestLog
from csp.rules import ELIGIBILITY_MIN_ACCOUNTS
from django.db.models import Count, Max, Sum
from django.utils import timezone

from bankflow import intelligence, services
from bankflow.models import (
    CodeResolution,
    StatementFile,
    StatementTransaction,
    UnresolvedFlowDaily,
)

ZERO = Decimal(0)
STALE_COUNT_DAYS = 3
LATE_AFTER_DAYS = 2  # a statement row first stored 2+ days after its own date

#: labels the business has said are NOT customer withdrawals or deposits (kept out on purpose)
APPROVED_EXCLUSIONS = {"unknown:outimps", "unknown:ppf", "unknown:yono fi wdl"}
#: held out until independently verified and approved
AWAITING_APPROVAL = {"unknown:yono withdrawal"}

CODE_WORDS = {
    "unmatched": "The code has the right shape but is not a known CSP.",
    "ambiguous_truncated": "The code is cut off or could belong to more than one CSP.",
    "variant_provisional": "The code has extra characters and the matching rule is not confirmed.",
    "missing": "The narration has no CSP code.",
    "pool_operations_no_csp": "Eko's own pool entries (deductions, cash funding): no CSP applies.",
}


def _cat(key, title, what, impact, count, next_action, *, amount=None, items=None,
         permitted="Read only. Ask an administrator to act on this.", info=False, note=""):
    return {
        "key": key, "title": title, "what": what, "impact": impact, "count": count,
        "amount": amount, "items": items or [], "next_action": next_action,
        "permitted": permitted, "note": note,
        "status": "info" if info else ("open" if count else "clear"),
    }


def _codes(date_from, date_to) -> dict:
    qs = UnresolvedFlowDaily.objects.filter(flow_date__gte=date_from, flow_date__lte=date_to)
    by_status = list(qs.values("status").annotate(
        n=Sum("txn_count"), credit=Sum("credit_amount"), debit=Sum("debit_amount")
    ).order_by("-n"))
    per = [{
        "status": r["status"], "words": CODE_WORDS.get(r["status"], r["status"]),
        "count": r["n"] or 0, "amount": (r["credit"] or ZERO) + (r["debit"] or ZERO),
        "expected": r["status"] == "pool_operations_no_csp",
    } for r in by_status]
    needs = [r for r in per if not r["expected"]]
    examples = list(
        CodeResolution.objects.filter(
            transaction__txn_date__gte=date_from, transaction__txn_date__lte=date_to,
            status__in=["unmatched", "ambiguous_truncated", "variant_provisional"],
        ).values("ko_raw", "status").annotate(
            n=Count("id"), credit=Sum("transaction__credit"), debit=Sum("transaction__debit")
        ).order_by("-n")[:25]
    )
    share = intelligence.dashboard_summary(date_from, date_to)["flows"]["unattributed_share"]
    count = sum(r["count"] for r in needs)
    amount = sum((r["amount"] for r in needs), ZERO)
    return _cat(
        "codes", "CSP could not be identified",
        "Statement rows whose CSP code is unknown, cut off or ambiguous. A 7-character code is "
        "never valid; a 9-character code belongs to its 8-character parent.",
        "These rows are left out of every CSP's figures. Nothing is guessed or assigned.",
        count, "Check the code with the data team. A row is only counted after a verified mapping "
        "exists.",
        amount=amount, items=[{"kind": "status", **r} for r in per] + [
            {"kind": "example", "ko": r["ko_raw"], "status": r["status"], "count": r["n"],
             "amount": (r["credit"] or ZERO) + (r["debit"] or ZERO)} for r in examples],
        note=(f"About {share:.0%} of statement value in this period could not be linked to a CSP."
              if share else ""),
    )


def _labels(date_from, date_to) -> dict:
    groups = list(
        StatementTransaction.objects.filter(
            category="unclassified", txn_date__gte=date_from, txn_date__lte=date_to
        ).values("channel_normalized").annotate(
            n=Count("id"), credit=Sum("credit"), debit=Sum("debit")).order_by("-n")
    )
    items = []
    for g in groups:
        label = g["channel_normalized"]
        decision = ("held out until approved" if label in AWAITING_APPROVAL
                    else "approved exclusion" if label in APPROVED_EXCLUSIONS
                    else "needs a decision")
        items.append({"label": label.removeprefix("unknown:") or "(no label)",
                      "count": g["n"], "amount": (g["credit"] or ZERO) + (g["debit"] or ZERO),
                      "decision": decision})
    needs = [i for i in items if i["decision"] != "approved exclusion"]
    return _cat(
        "labels", "Unrecognised transaction labels",
        "Statement rows with a label that is not an approved withdrawal or deposit type.",
        "They are never counted. Approved exclusions are listed for completeness.",
        sum(i["count"] for i in needs), "Ask the business whether any label should be counted. "
        "Until it is approved it stays out.",
        amount=sum((i["amount"] for i in needs), ZERO), items=items,
    )


def _duplicates(date_from, date_to) -> dict:
    qs = StatementTransaction.objects.filter(txn_date__gte=date_from, txn_date__lte=date_to)
    dup = qs.filter(is_duplicate_ref=True).count()
    rev = qs.filter(is_reversal_candidate=True)
    rev_agg = rev.aggregate(n=Count("id"), credit=Sum("credit"), debit=Sum("debit"))
    sample = [{
        "date": r.txn_date, "reference": r.ref_number, "label": r.channel_normalized,
        "amount": r.credit or r.debit, "reason": r.reversal_reason.replace("_", " "),
    } for r in rev.order_by("-txn_date")[:20]]
    return _cat(
        "duplicates", "Possible duplicates and reversals",
        f"{dup} rows share a bank reference with another row; {rev_agg['n']} rows run against "
        "their channel's normal direction (a possible reversal).",
        "Exact repeats are dropped when a file is read twice. Flagged rows stay in their day's "
        "totals as the bank posted them.",
        dup + (rev_agg["n"] or 0), "Compare the flagged references with the bank statement before "
        "relying on that day.", amount=(rev_agg["credit"] or ZERO) + (rev_agg["debit"] or ZERO),
        items=sample,
    )


def _counts(today: dt.date) -> dict:
    csps = list(Csp.objects.tracked().values("csp_code", "name", "account_count"))
    last = {
        r["csp_id"]: r["m"] for r in DailyBalance.objects.exclude(
            source__in=intelligence.NOT_OBSERVATIONS
        ).values("csp_id").annotate(m=Max("balance_date"))
    }
    items = []
    for c in csps:
        issues = []
        n = c["account_count"]
        if n is None:
            issues.append("missing")
        elif n == 0:
            issues.append("zero")
        seen = last.get(c["csp_code"])
        if seen is None:
            issues.append("never read")
        elif (today - seen).days > STALE_COUNT_DAYS:
            issues.append(f"stale ({(today - seen).days} days)")
        if issues:
            items.append({"csp_code": c["csp_code"], "name": c["name"], "count": n,
                          "last_reading": seen, "issue": ", ".join(issues)})
    below = sum(
        1 for c in csps
        if c["account_count"] and c["account_count"] < ELIGIBILITY_MIN_ACCOUNTS
    )
    items.sort(key=lambda i: (i["issue"].startswith("stale"), i["csp_code"]))
    return _cat(
        "counts", "Missing, zero or stale account counts",
        "CSPs whose account count is missing, zero, or has not been read for more than "
        f"{STALE_COUNT_DAYS} days.",
        "The last known count keeps being used and the affected estimates are provisional. A zero "
        "or non-numeric count is invalid and needs review.",
        len(items), "Check the CSP's 'Total Accounts' cell in the Calling Sheet.", items=items[:60],
        note=f"{below} CSPs are below the {ELIGIBILITY_MIN_ACCOUNTS}-account eligibility gate "
        "(that is a rule, not a data problem).",
    )


def _coverage(date_from, date_to, today) -> dict:
    cov = services.day_coverage()
    items = []
    day = date_from
    while day <= date_to:
        e = cov.get(day)
        if e is None:
            if day < today:
                items.append({"date": day, "state": "no statement", "detail":
                              "No bank statement covers this day."})
        elif e["class"] != services.COMPLETE_DAY:
            items.append({"date": day, "state": "partial" if e["class"] == services.PARTIAL_DAY
                          else "unverified", "detail": e["reason"]})
        day += dt.timedelta(days=1)
    return _cat(
        "coverage", "Statement coverage gaps",
        "Days with no bank statement, or with one that is not proven complete. No transactions "
        "is different from no statement.",
        "Estimates carry forward through a gap and are marked provisional.",
        len(items), "Load the missing statement. Alerts about money going out resume on their own.",
        items=list(reversed(items))[:40],
    )


def _late(date_from, date_to) -> dict:
    files = {f["id"]: timezone.localtime(f["imported_at"]).date()
             for f in StatementFile.objects.values("id", "imported_at")}
    groups = StatementTransaction.objects.filter(
        txn_date__gte=date_from, txn_date__lte=date_to
    ).values("first_statement_id", "txn_date").annotate(
        n=Count("id"), credit=Sum("credit"), debit=Sum("debit"))
    by_day: dict = defaultdict(lambda: [0, ZERO])
    for g in groups:
        arrived = files.get(g["first_statement_id"])
        if arrived and (arrived - g["txn_date"]).days >= LATE_AFTER_DAYS:
            by_day[g["txn_date"]][0] += g["n"]
            by_day[g["txn_date"]][1] += (g["credit"] or ZERO) + (g["debit"] or ZERO)
    items = [{"date": d, "count": v[0], "amount": v[1]} for d, v in sorted(by_day.items())]
    return _cat(
        "late", "Late-arriving transactions",
        f"Statement rows first stored {LATE_AFTER_DAYS} or more days after their own date.",
        "They are flagged only. Earlier days are never recalculated automatically.",
        sum(i["count"] for i in items), "Nothing to do unless a day's total looks wrong; then "
        "ask for a reviewed correction.", amount=sum((i["amount"] for i in items), ZERO),
        items=items[-30:], info=True,
    )


def _failed(date_from, is_staff: bool) -> dict:
    files = [{
        "kind": "file", "name": f.filename, "status": f.status, "when": f.imported_at,
        "reason": f.status_reason if is_staff else "",
    } for f in StatementFile.objects.exclude(status=StatementFile.Status.IMPORTED)
        .order_by("-imported_at")[:15]]
    logs = IngestLog.objects.filter(
        status=IngestLog.Status.FAILED, started_at__date__gte=date_from
    ).exclude(source="calling_sheet").order_by("-started_at")[:15]
    items = files + [{
        "kind": "run", "name": run.source.replace("_", " "), "status": "failed",
        "when": run.started_at, "reason": (run.error_summary[:200] if is_staff else ""),
    } for run in logs]
    cs_failed = IngestLog.objects.filter(
        source="calling_sheet", status=IngestLog.Status.FAILED, started_at__date__gte=date_from
    ).count()
    return _cat(
        "failed", "Failed or quarantined imports",
        "Statement files that were set aside, and data runs that failed in this period.",
        "A set-aside file contributes no transactions. A failed run leaves the last good data "
        "in place.", len(files) + len(logs) + (1 if cs_failed else 0),
        "An administrator can look at the reason and re-run the step.",
        items=items, permitted=("Staff can see the reason. Re-running a step is under "
                                "Operations > System health." if is_staff else
                                "Read only. Technical reasons are shown to staff."),
        note=(f"The Calling Sheet read failed {cs_failed} times in this period."
              if cs_failed else ""),
    )


def _drift(today: dt.date) -> dict:
    from bankflow import corrections

    drifted = corrections.estimate_drift(today=today)
    items = [{"csp_code": i["csp_code"], "date": i["date"],
              "stored_net": i["stored_deposits"] - i["stored_withdrawals"],
              "current_net": i["current_deposits"] - i["current_withdrawals"],
              "difference": i["net_difference"]} for i in drifted[-40:]]
    return _cat(
        "drift", "Finished estimates built on older statement data",
        "A day's estimate was finalised, then the bank statement data for that day changed.",
        "The stored estimate for that day (and the days after it) still uses the older data. It "
        "is not changed automatically.",
        len(drifted), "An administrator decides whether to correct it, from the server "
        "(manage.py correct_estimate). A reason is stored with the old values.",
        items=items, permitted="Read only here. A correction needs a superuser on the server.",
    )


def review_queue(days: int = 30, *, is_staff: bool = False, today: dt.date | None = None) -> dict:
    today = today or timezone.localdate()
    date_from, date_to = intelligence.resolve_range(days, date_to=today)
    cats = [
        _codes(date_from, date_to), _labels(date_from, date_to),
        _duplicates(date_from, date_to), _counts(today),
        _coverage(date_from, date_to, today), _late(date_from, date_to),
        _drift(today), _failed(date_from, is_staff),
    ]
    exc = intelligence.exceptions(date_from, date_to, limit=500)
    csp_items = [i for i in exc["items"] if i["csp_code"]]
    network = [i for i in exc["items"] if not i["csp_code"]]
    return {
        "range": {"from": date_from, "to": date_to, "days": days},
        "categories": cats,
        "open": sum(1 for c in cats if c["status"] == "open"),
        "csp_alert_count": len(csp_items),
        "csp_alerts_by_type": exc["by_type"],
        "network_notes": network,
        "controls": {
            "can_clear_flags": False,
            "text": "This page only shows what the data says. Marking something reviewed does not "
                    "change any figure, and no correction can be made from here.",
        },
    }


__all__ = ["review_queue"]
