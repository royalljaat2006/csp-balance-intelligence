"""
"Today vs yesterday" in plain words: the view model behind components/day_compare.html.

This module only RE-READS what `bankflow.intelligence.day_compare` already worked out (its rows,
money states and the extras added for this view). It does no business calculation of its own: every
number shown comes from that payload, a missing figure stays None (and is shown as a sentence, never
as 0), and a money comparison is made only when the backend says both days cover the same period.
It is the single place the wording lives, so the template stays plain markup and nothing is
computed in JavaScript.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from .templatetags.fmt import _indian, inr

# Plain names for the statement categories the backend may report as "not counted".
EXCLUDED_WORDS = {
    "transfer": "Money sent from one account to another",
    "offus": "Cash taken out by customers of other banks",
    "other_product": "Loan and savings-scheme (SSA) deposits",
    "pool_operations": "Eko's own cash deposits and fees",
    "unclassified": "Money movements we do not recognise yet",
}

UP, DOWN, SAME, WAITING, UNKNOWN = "up", "down", "same", "waiting", "unknown"
_TONE = {UP: "ok", DOWN: "warn", SAME: "", WAITING: "info", UNKNOWN: "info"}
_ICON = {UP: "up", DOWN: "down", SAME: "same", WAITING: "wait", UNKNOWN: "wait"}


def _count(n: int) -> str:
    return _indian(int(abs(n)))


def _status(kind: str, words: str, *, tone: str | None = None) -> dict:
    return {"kind": kind, "words": words, "tone": _TONE[kind] if tone is None else tone,
            "icon": _ICON[kind]}


def _compare(row: dict, *, unit: str, what: str = "", neutral: bool = False,
             comparable: bool = True, waiting: str = "") -> dict:
    """Up / down / same / waiting for one backend row. `comparable` False means the two days do
    not cover the same period, so no comparison is made; `waiting` then says why."""
    t, y, change = row["today"], row["yesterday"], row["change"]
    if t is None:
        return _status(WAITING if waiting else UNKNOWN,
                       waiting or "Not enough information yet")
    if y is None or change is None:
        return _status(UNKNOWN, "Not ready to compare yet")
    if not comparable:
        return _status(WAITING, waiting or "Not ready to compare yet")
    if change == 0:
        return _status(SAME, "No change")
    amount = inr(abs(change)) if unit == "rupees" else _count(change)
    if change > 0:
        return _status(UP, f"{amount} more than yesterday",
                       tone="" if neutral else None)
    return _status(DOWN, f"{amount} fewer than yesterday" if unit != "rupees"
                   else f"{amount} less than yesterday", tone="" if neutral else None)


def _missing_today(state: str) -> str:
    return {"no_data": "Today's report has not arrived"}.get(
        state, "No figure was saved for today")


def friendly(dc: dict | None) -> dict | None:
    """The plain-language model for one `day_compare` payload (None when there is none)."""
    if not dc:
        return None
    rows = {r["key"]: r for r in dc["rows"]}
    ms = dc.get("money_state") or {}
    t_state, y_state = ms.get("today", ""), ms.get("yesterday", "")
    like = bool(dc.get("like_for_like_money"))
    is_csp = dc.get("scope") == "csp"

    # ------------------------------------------------------------ A: accounts
    def count_card(key, title, icon, note=""):
        r = rows[key]
        return {
            "key": key, "title": title, "icon": icon, "unit": "count", "today": r["today"],
            "yesterday": r["yesterday"], "missing": _missing_today(""),
            "status": _compare(r, unit="count"), "note": note,
        }

    rep = dc.get("accounts_reporting") or {}
    acc_note = ""
    if rep.get("today") != rep.get("yesterday") and rows["accounts"]["today"] is not None:
        acc_note = ("Some centres sent their numbers on only one of the two days, so the change "
                    "counts only centres that sent both.")
    accounts = [
        count_card("accounts", "How many accounts are there?", "people", acc_note),
        count_card("new_accounts_mtd", "New accounts this month", "plus",
                   "A new month has just started, so this count began again from zero."
                   if rows["new_accounts_mtd"]["note"].startswith("A new month") else ""),
        count_card("new_accounts_day", "Accounts opened today", "sun",
                   "Today's number can still grow."),
    ]
    # an in-progress day is not a trend: show the difference but do not colour it
    accounts[2]["status"] = {**accounts[2]["status"],
                             "tone": "" if accounts[2]["status"]["kind"] in (UP, DOWN) else
                             accounts[2]["status"]["tone"]}

    # ------------------------------------------------------------ B: money movement
    def money_card(key, title, icon, ask):
        r = rows[key]
        partial_today = r["today"] is not None and t_state != "complete"
        return {
            "key": key, "title": title, "icon": icon, "ask": ask, "unit": "rupees",
            "today": r["today"], "yesterday": r["yesterday"],
            "missing": _missing_today(t_state),
            "yesterday_missing": ("no report was saved" if y_state in ("no_data", "")
                                  else "no figure was saved"),
            "today_partial": partial_today, "yesterday_partial": (
                r["yesterday"] is not None and y_state != "complete"),
            "status": _compare(
                r, unit="rupees", neutral=True, comparable=like,
                waiting="Waiting for today's report" if t_state != "complete"
                else "Not ready to compare yet"),
        }

    money = [
        money_card("withdrawals", "Money taken out today", "out",
                   "Cash that customers took out of their accounts."),
        money_card("deposits", "Money put in today", "in",
                   "Cash that customers put into their accounts."),
    ]
    txn = rows["transactions"]
    transactions = {"today": txn["today"], "yesterday": txn["yesterday"],
                    "missing": ("Waiting for today's report" if t_state == "no_data"
                                else "No figure was saved for today")}

    notice: list[str] = []
    if t_state == "no_data":
        notice.append("Today's report has not arrived yet.")
    elif t_state != "complete":
        notice.append("Today's numbers may change as more information arrives.")
    if y_state == "no_data":
        notice.append("No report was saved for yesterday.")
    elif y_state != "complete":
        notice.append("Yesterday's report covers only part of the day, so the totals may not "
                      "be directly comparable.")

    excluded = [
        {"words": EXCLUDED_WORDS.get(e["category"], e["category"].replace("_", " ").capitalize()),
         "today": e["today"], "yesterday": e["yesterday"]}
        for e in dc.get("excluded") or []
    ]

    # ------------------------------------------------------------ C: money per account
    obs, est = rows["observed_avg"], rows["estimated_avg"]
    sent = dc.get("sbi_last_sent")
    sbi = {
        "title": "Average money per account (SBI)", "today": obs["today"],
        "yesterday": obs["yesterday"], "last_sent": sent,
        "missing": "SBI has not sent a figure yet",
        "status": _compare(obs, unit="rupees", neutral=True),
        "note": "This number changes when SBI sends a new figure.",
    }
    ours = {
        "title": "Our estimated average per account", "today": est["today"],
        "yesterday": est["yesterday"],
        "missing": "We need more data before we can make a reliable estimate.",
        "status": _compare(est, unit="rupees", neutral=True),
        "note": "This is our own guess. It is not a figure from SBI.",
        "link": (reverse("dashboard:csp_explain", args=[dc["csp_code"]])
                 if is_csp and dc.get("csp_code") else reverse("dashboard:csp_directory")),
        "link_words": ("How did we calculate this?" if is_csp and dc.get("csp_code")
                       else "Pick a CSP to see how its estimate is worked out"),
    }

    # ------------------------------------------------------------ D: what changed?
    def item(title, st, extra=""):
        return {"title": title, "words": st["words"] + (f" ({extra})" if extra else "")}

    entries: list[tuple[str, dict, str]] = [
        ("All accounts", accounts[0]["status"], ""),
        ("New accounts this month", accounts[1]["status"], ""),
        ("Accounts opened today", accounts[2]["status"], "so far"),
        ("Money taken out", money[0]["status"], ""),
        ("Money put in", money[1]["status"], ""),
        ("SBI's average per account", sbi["status"], ""),
        ("Our estimated average", ours["status"], ""),
    ]
    if txn["today"] is not None and txn["yesterday"] is not None and like:
        entries.insert(5, ("Transactions counted",
                           _compare(txn, unit="count", comparable=like), ""))
    changes: dict[str, list[dict]] = {UP: [], DOWN: [], SAME: [], WAITING: []}
    for title, st, extra in entries:
        bucket = WAITING if st["kind"] == UNKNOWN else st["kind"]
        if st["kind"] == UNKNOWN and title == "Our estimated average":
            st = {**st, "words": "We need more data first"}
        changes[bucket].append(item(title, st, extra if st["kind"] in (UP, DOWN) else ""))

    # ------------------------------------------------------------ why numbers are missing
    why = []
    if t_state != "complete":
        why.append("Some reports arrive later than others. Today's bank report "
                   f"{'has not arrived yet' if t_state == 'no_data' else 'is still coming in'}, "
                   "so today's money numbers are not final.")
    if y_state not in ("complete",):
        why.append("Yesterday's bank report "
                   f"{'was never saved' if y_state == 'no_data' else 'covers only part of the day'}"
                   ", so we do not compare money between the two days.")
    if excluded:
        why.append("Some money movements are left out on purpose, such as the ones in "
                   "\"What is not included?\" above.")
    why.append("Some figures come straight from SBI. They change only when SBI sends a new "
               "number.")
    if est["today"] is None:
        why.append("Other figures are our own estimates. An estimate needs enough history before "
                   "we can trust it, so it stays empty until then.")

    upd = dc.get("updated") or {}
    sheet_at = upd.get("sheet_at")
    sheet_old = sheet_at is not None and (timezone.now() - sheet_at).total_seconds() > 26 * 3600
    ot = dc.get("other_types") or {}
    excluded_known = any((ot.get(k) or {}).get("txn_count") is not None
                         for k in ("today", "yesterday"))
    return {
        "today": dc["today"], "yesterday": dc["yesterday"], "scope": dc.get("scope"),
        "accounts": accounts, "money": money, "transactions": transactions,
        "notice": notice, "excluded": excluded, "excluded_known": excluded_known,
        "sbi": sbi, "ours": ours,
        "changes": [
            (UP, "Went up", "up", changes[UP]), (DOWN, "Went down", "down", changes[DOWN]),
            (SAME, "No change", "same", changes[SAME]),
            (WAITING, "Waiting for data", "wait", changes[WAITING]),
        ],
        "why": why,
        "updated": {"sheet_at": sheet_at, "sheet_old": sheet_old,
                    "statements_at": upd.get("statements_at"),
                    "statements_day": upd.get("statements_day")},
    }

