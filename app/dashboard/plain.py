"""
Plain-English layer for the dashboard: turns the technical payloads from
bankflow.intelligence into short sentences a non-technical operations manager can act on.

Presentation only. Every number here comes from the payloads it is given; nothing is
computed, estimated or invented, and technical vocabulary stays in the API / Advanced details.

Terminology (agreed): net pool flow -> "Money in minus money out"; PMJDY account growth ->
"New customer accounts"; observed Avg Balance -> "SBI-reported average balance"; estimated Avg
Balance -> "Our estimated average balance"; INSUFFICIENT_EVIDENCE -> "Reliable estimate not
available yet"; unmatched CSP -> "CSP could not be identified"; exceptions -> "Needs attention".
"""

from __future__ import annotations

from .templatetags.fmt import inr, intc, signed

ESTIMATE_UNAVAILABLE = "Reliable estimate not available yet"

STATUS_WORDS = {
    "VALIDATED": "Checked against past data",
    "PROVISIONAL": "Early estimate, still being checked",
    "INSUFFICIENT_EVIDENCE": ESTIMATE_UNAVAILABLE,
    "UNAVAILABLE": "No estimate for this CSP",
    "NOT_COMPUTED": "Not calculated yet",
    "NOT_RUN": "Not checked yet",
    "NOT_SUPPORTED": "Did not hold up against past data",
}

SEVERITY_WORDS = {"high": "Check today", "medium": "Check this week", "low": "For your information"}

#: type -> (headline, what it means, what to do). Wording avoids jargon and never claims a cause.
FRIENDLY = {
    "NEGATIVE_NET_FLOW": (
        "Much more money went out than came in",
        "A large share of this CSP's money movement went out of the pool instead of coming in. "
        "That can be normal, so it is a reason to look, not proof of a problem.",
        "Ask the CSP or territory lead what happened this period.",
    ),
    "GROWTH_WITHOUT_FLOW_EVIDENCE": (
        "Many new accounts, but we cannot see their activity",
        "This CSP added a lot of customer accounts, but we are missing transaction data for much "
        "of the period, so we cannot tell how the money moved.",
        "Check that the latest bank statements have been loaded.",
    ),
    "ACCOUNT_COUNT_DECREASED": (
        "Customer account count went down",
        "The latest account count is lower than before. Counts are normally not expected to fall.",
        "Check this CSP's entry in the account-count sheet.",
    ),
    "POSSIBLE_ESTIMATED_DECLINE": (
        "Average balance may be falling",
        "Our estimate is below the SBI-reported average balance. It is an estimate, not a "
        "confirmed balance.",
        "Wait for the next SBI update to confirm, or ask the CSP.",
    ),
    "STATEMENT_COVERAGE_LOW": (
        "Alerts about money going out are paused",
        "The bank statements loaded cover too little of this period to compare money in and out "
        "fairly, so those alerts are held back instead of guessing.",
        "Load the missing statements. Alerts resume on their own.",
    ),
    "STALE_OR_MISSING_SOURCE": (
        "Information is out of date",
        "We have no recent account count for this CSP.",
        "Check that the account-count sheet is being read.",
    ),
    "UNATTRIBUTED_FLOW": (
        "Some transactions could not be linked to a CSP",
        "Part of the bank statement could not be matched to any CSP, so it is left out of every "
        "CSP's figures.",
        "Ask the data team to review unmatched codes. Do not guess a match.",
    ),
    "INSUFFICIENT_HISTORY": (
        ESTIMATE_UNAVAILABLE,
        "We do not yet have enough past data to estimate average balances reliably, so the "
        "SBI-reported figure is shown instead.",
        "Nothing to do now. The estimate appears once enough history is loaded.",
    ),
}


def friendly_exception(item: dict, days: int | None = None) -> dict:
    title, meaning, action = FRIENDLY.get(
        item["type"],
        (item["type"].replace("_", " ").capitalize(), item["reason"], item["next_action"]),
    )
    m = item.get("metrics") or {}
    facts: list[str] = []
    if "net_flow" in m:
        facts.append(f"{inr(abs(m['net_flow']))} more went out than came in")
    if "imbalance_pct" in m:
        facts.append(f"{m['imbalance_pct']:g}% of this CSP's activity")
    if "new_accounts" in m:
        facts.append(f"{m['new_accounts']} new accounts")
    if "coverage" in m:
        facts.append(f"statements cover {m['coverage']:.0%} of the days")
    if "growth_pct" in m:
        facts.append(f"{m['growth_pct']:+.1f}% customer accounts")
    if "previous" in m and "current" in m:
        facts.append(f"accounts {intc(m['previous'])} → {intc(m['current'])}")
    if "estimated_delta" in m:
        facts.append(f"estimate {signed(round(m['estimated_delta']))} per account vs SBI")
    if "csps" in m:
        facts.append(f"{m['csps']} CSPs")
    if "unattributed_amount" in m:
        facts.append(f"{inr(m['unattributed_amount'])} unmatched")
    cov = item.get("evidence_coverage")
    return {
        "type": item["type"], "severity": item["severity"],
        "severity_label": SEVERITY_WORDS[item["severity"]],
        "title": title, "meaning": meaning, "action": action, "facts": facts,
        "csp_code": item.get("csp_code"), "name": item.get("name"),
        "data_note": (
            f"Based on {cov:.0%} of the days in this period" if cov is not None and cov < 1 else ""
        ),
    }


def daily_summary(summary: dict, attention_total: int, high: int, days: int) -> list[str]:
    """3-5 short sentences, each a statement of real numbers from `summary`."""
    out: list[str] = []
    a, f, e = summary["accounts"], summary["flows"], summary["estimates"]
    na = summary["new_accounts"]
    if na["available"] and na["mtd"] is not None:
        today = f" ({intc(na['today'])} on the latest day)" if na["today"] else ""
        out.append(
            f"There are {intc(a['current_total'])} customer accounts. {intc(na['mtd'])} new "
            f"customer accounts were opened so far in {na['month_label']}{today}."
        )
    elif a["growth"] is not None:
        out.append(
            f"There are {intc(a['current_total'])} customer accounts, "
            f"{intc(a['growth']) if a['growth'] >= 0 else intc(abs(a['growth']))} "
            f"{'new' if a['growth'] >= 0 else 'fewer'} in the last {days} days."
        )
    else:
        out.append(
            f"There are {intc(a['current_total'])} customer accounts. We do not have an earlier "
            "count yet to show how many are new."
        )
    if f["days_with_data"]:
        diff = f["net_pool_flow"]
        word = "more came in than went out" if diff >= 0 else "more went out than came in"
        out.append(
            f"From withdrawals and deposits, {inr(f['pool_credits'])} came into the pool and "
            f"{inr(f['pool_debits'])} went out: {inr(abs(diff))} {word}."
        )
    else:
        out.append("No bank statements cover this period yet, so money movement is not shown.")
    ne = e["network_estimate"]
    if ne:
        out.append(
            f"Our estimated average balance is {inr(ne['value'])} per account "
            f"({STATUS_WORDS[ne['status']].lower()})."
        )
    else:
        out.append(f"{ESTIMATE_UNAVAILABLE}. The SBI-reported average balance is shown instead.")
    if attention_total:
        urgent = f", {high} to check today" if high else ""
        noun = "item needs" if attention_total == 1 else "items need"
        out.append(f"{attention_total} {noun} attention{urgent}.")
    else:
        out.append("Nothing needs attention right now.")
    return out


def data_notes(summary: dict) -> list[dict]:
    """Plain warnings about stale or incomplete data; empty when everything is current."""
    notes: list[dict] = []
    fr, f = summary["freshness"], summary["flows"]
    na = summary["new_accounts"]
    if not na["available"]:
        notes.append({"level": "warn", "text": (
            f"New customer accounts for {na['month_label']} have not been captured yet, so the "
            "number of new accounts is not shown.")})
    elif fr["pmjdy"]["state"] == "stale":
        notes.append({"level": "warn", "text": (
            "New customer account numbers were last updated "
            f"{fr['pmjdy']['age_hours']} hours ago.")})
    if fr["calling_sheet"]["state"] in ("stale", "delayed", "missing"):
        notes.append({"level": "warn", "text": "SBI-reported balances may be out of date."})
    if f["range_days"] and f["days_with_data"] < f["range_days"]:
        notes.append({"level": "info", "text": (
            f"Money movement covers {f['days_with_data']} of the last {f['range_days']} days; "
            "totals are incomplete.")})
    if f["unattributed_share"]:
        notes.append({"level": "info", "text": (
            f"About {f['unattributed_share']:.0%} of statement value could not be linked to a CSP "
            "and is not included in any CSP's figures.")})
    return notes


def csp_status(row: dict) -> tuple[str, str]:
    """('ok'|'attention', short reason) for the table's Status column."""
    if row.get("flags"):
        rank = {"high": 0, "medium": 1, "low": 2}
        top = sorted(row["flags"], key=lambda f: rank[f["severity"]])[0]
        return "attention", FRIENDLY.get(top["type"], (top["type"],))[0]
    return "ok", "Nothing to flag"


def estimate_cell(row: dict) -> dict:
    ok = (
        row["estimate_status"] in ("VALIDATED", "PROVISIONAL")
        and row["estimated_value"] is not None
    )
    return {
        "show": ok, "value": row["estimated_value"], "delta": row["estimated_delta"],
        "label": STATUS_WORDS.get(row["estimate_status"], ESTIMATE_UNAVAILABLE) if ok
        else ESTIMATE_UNAVAILABLE,
    }
