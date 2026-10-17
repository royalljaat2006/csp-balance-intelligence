"""
Data readiness -- the journey of the data from "received" to "can support an estimate", read-only.

Two different questions are answered separately on purpose:

  * did the import work technically?   (files read, no failures)
  * is the data complete enough for a reliable estimate?   (statement coverage, attribution,
    account counts, and whether the estimator has been validated)

A green answer to the first never implies the second.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from csp.models import Csp, IngestLog
from django.conf import settings
from django.db.models import Count, Max, Sum
from django.utils import timezone

from bankflow import intelligence, services
from bankflow.models import (
    DailyEstimate,
    HypothesisRun,
    ImportRun,
    StatementFile,
    StatementTransaction,
)

ZERO = Decimal(0)
COUNTED = ("withdrawal", "deposit")


def _stage(key, number, title, status, headline, facts, *, next_action="", problems=None):
    return {"key": key, "number": number, "title": title, "status": status,
            "headline": headline, "facts": facts, "next_action": next_action,
            "problems": problems or []}


def data_readiness(*, is_staff: bool = False, days: int = 30,
                   today: dt.date | None = None) -> dict:
    today = today or timezone.localdate()
    date_from, date_to = intelligence.resolve_range(days, date_to=today)
    now = timezone.now()
    ingest = services.ingestion_summary()
    cov = services.coverage_report()
    res = services.code_resolution_stats()
    quality = services.quality_flags()
    summary = intelligence.dashboard_summary(date_from, date_to)

    # 1. source data received ------------------------------------------------------------------
    fr = summary["freshness"]
    sources = []
    for key, label, when in (
        ("calling_sheet", "Calling Sheet (SBI average balance, account counts)",
         fr["calling_sheet"]["last_success"]),
        ("pmjdy", "New customer accounts (Calling Sheet monthly blocks)",
         fr["pmjdy"]["last_observed"]),
        ("transactions", "Bank statements (pool account)", fr["transactions"]["last_processed"]),
        ("estimates", "Daily estimates", fr["estimates"]["calculated_at"]),
    ):
        sources.append({"key": key, "label": label, "received_at": when,
                        "state": fr[key]["state"], "age_hours": fr[key].get("age_hours")})
    last_stmt = ingest["latest_statement"]
    stale = [s["label"] for s in sources if s["state"] in ("stale", "missing")]
    s1 = _stage(
        "received", 1, "Source data received",
        "bad" if stale else "ok",
        ("Everything arrived recently." if not stale else
         f"{len(stale)} source(s) are out of date or have never arrived."),
        {"sources": sources,
         "latest_statement_day": last_stmt["statement_end"] if last_stmt else None,
         "latest_statement_file": last_stmt["filename"] if last_stmt else None},
        next_action="" if not stale else "Check that the data feeds are running (Operations).",
        problems=stale,
    )

    # 2. file and schema validation -------------------------------------------------------------
    files = ingest["files"]
    bad_files = list(
        StatementFile.objects.exclude(status=StatementFile.Status.IMPORTED).order_by(
            "-imported_at")[:8]
    )
    workbook = IngestLog.objects.filter(source="transactions").order_by("-started_at").first()
    s2 = _stage(
        "validated", 2, "File and format checks",
        "bad" if files["failed"] else "warn" if files["quarantined"] else "ok",
        (f"{files['imported']} statement file(s) accepted, {files['quarantined']} set aside, "
         f"{files['failed']} unreadable."),
        {"files": files, "last_import_run": ingest["last_import_run"],
         "reconciliation": ingest["reconciliation_status_counts"],
         "latest_reconciliation": (last_stmt or {}).get("reconciliation"),
         "latest_header_gap": (last_stmt or {}).get("header_gap"),
         "set_aside": [{"name": f.filename, "status": f.status, "when": f.imported_at,
                        "reason": f.status_reason if is_staff else ""} for f in bad_files],
         "workbook": ({"status": workbook.status, "when": workbook.started_at,
                       "rows_read": workbook.rows_read, "rows_rejected": workbook.rows_rejected,
                       "reason": workbook.error_summary if is_staff else ""}
                      if workbook else None)},
        next_action="" if not (files["failed"] or files["quarantined"])
        else "An administrator should look at the set-aside files.",
        problems=[f.filename for f in bad_files],
    )

    # 3. duplicate detection --------------------------------------------------------------------
    runs = ImportRun.objects.aggregate(dup=Sum("files_duplicate"), seen=Sum("files_seen"))
    s3 = _stage(
        "duplicates", 3, "Duplicate checks",
        "warn" if quality["duplicate_reference_rows"] else "ok",
        f"{quality['overlap_rows_skipped']:,} repeated rows were ignored; "
        f"{quality['duplicate_reference_rows']:,} rows share a bank reference and are flagged.",
        {"files_seen_again": runs["dup"] or 0, "overlapping_rows_ignored":
         quality["overlap_rows_skipped"], "duplicate_reference_rows":
         quality["duplicate_reference_rows"], "reversal_candidates":
         quality["reversal_candidate_rows"]},
        next_action="Flagged rows are listed under Review.",
    )

    # 4. CSP identification and classification --------------------------------------------------
    by = {r["status"]: r for r in res["by_status"]}
    exact = by.get("exact", {})
    unclass = quality["unclassified_type_rows"]
    s4 = _stage(
        "identified", 4, "CSP and transaction type identified",
        "warn" if (res["total_transactions"] and (exact.get("count_share") or 0) < 0.99
                   or unclass) else "ok",
        ("No statement rows yet." if not res["total_transactions"] else
         f"{(exact.get('count_share') or 0):.1%} of rows are linked to a CSP "
         f"({(exact.get('amount_share') or 0):.1%} of the money)."),
        {"by_status": res["by_status"], "unclassified_rows": unclass,
         "trimmed_to_8": res["trimmed_to_8_chars"], "rule": res["code_format_rule"]},
        next_action="Unlinked rows and unrecognised labels are listed under Review.",
    )

    # 5. accepted, excluded and quarantined -----------------------------------------------------
    cats = {r["category"]: r for r in StatementTransaction.objects.filter(
        txn_date__gte=date_from, txn_date__lte=date_to
    ).values("category").annotate(n=Count("id"), credit=Sum("credit"), debit=Sum("debit"))}

    def n_amt(names):
        rs = [cats[c] for c in names if c in cats]
        return (sum(r["n"] for r in rs), sum(((r["credit"] or ZERO) + (r["debit"] or ZERO))
                                             for r in rs))

    counted_n, counted_amt = n_amt(COUNTED)
    excl_n, excl_amt = n_amt(("transfer", "offus", "other_product", "pool_operations"))
    uncl_n, uncl_amt = n_amt(("unclassified",))
    unattr = summary["flows"]["unattributed_amount"]
    s5 = _stage(
        "classified", 5, "Counted, excluded and set aside",
        "ok" if counted_n else "warn",
        f"{counted_n:,} withdrawals and deposits are eligible; {excl_n:,} rows are excluded by "
        f"rule; {uncl_n:,} have no approved label.",
        {"eligible": {"count": counted_n, "amount": counted_amt},
         "excluded_by_rule": {"count": excl_n, "amount": excl_amt},
         "unclassified": {"count": uncl_n, "amount": uncl_amt},
         "unattributed_amount": unattr,
         "unattributed_share": summary["flows"]["unattributed_share"],
         "range": {"from": date_from, "to": date_to}},
        next_action="Excluded types and unlinked rows never affect an estimate.",
    )

    # 6. estimates and the resulting data quality ----------------------------------------------
    est = summary["estimates"]
    estimator = est["estimator"]
    latest = DailyEstimate.objects.aggregate(d=Max("estimate_date"), c=Max("calculated_at"))
    s6 = _stage(
        "estimates", 6, "Estimates and data quality",
        "ok" if estimator["status"] == "VALIDATED" else "warn",
        (f"{est['csps_with_estimate']} of {summary['csps_tracked']} CSPs have an estimate; "
         f"estimator: {estimator['status'].replace('_', ' ').lower()}."),
        {"by_status": est["by_status"], "estimate_date": latest["d"],
         "calculated_at": latest["c"], "below_gate": est["below_200_account_gate"],
         "estimator": estimator},
        next_action="Estimates stay provisional until the estimator passes validation.",
    )
    stages = [s1, s2, s3, s4, s5, s6]
    latest_run = HypothesisRun.objects.first()

    # technical success vs fit for a reliable estimate ----------------------------------------
    technical_ok = not files["failed"] and not stale
    reasons: list[str] = []
    if cov["missing_date_ranges"]:
        reasons.append("Some days have no bank statement: " + ", ".join(
            cov["missing_date_ranges"][:4]) + ("…" if len(cov["missing_date_ranges"]) > 4 else ""))
    if cov["partial_days"] or cov["unverified_days"]:
        reasons.append(f"{cov['partial_days']} partial and {cov['unverified_days']} unverified "
                       "statement day(s).")
    flows_cov = summary["flows"]["coverage"]
    if flows_cov < settings.ALERT_MIN_NETWORK_COVERAGE:
        reasons.append(f"Statements cover only {flows_cov:.0%} of the last {days} days.")
    share = summary["flows"]["unattributed_share"]
    if share and share > 0.05:
        reasons.append(f"{share:.0%} of statement value could not be linked to a CSP.")
    if estimator["status"] != "VALIDATED":
        reasons.append("The estimator has not been validated against SBI refreshes.")
    reliable = technical_ok and not reasons
    return {
        "range": {"from": date_from, "to": date_to, "days": days}, "today": today, "now": now,
        "stages": stages,
        "verdict": {
            "technical": "ok" if technical_ok else "attention",
            "technical_text": ("Imports ran without technical failures." if technical_ok else
                               "An import failed or a source is out of date."),
            "reliable": "ok" if reliable else "provisional",
            "reliable_text": ("Data is complete enough for reliable estimates." if reliable else
                              "Estimates are provisional."),
            "reasons": reasons,
        },
        "csps_tracked": Csp.objects.tracked().count(),
        "hypothesis_run_at": latest_run.created_at if latest_run else None,
    }
