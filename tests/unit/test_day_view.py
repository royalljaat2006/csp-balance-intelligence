"""The plain-language "Today vs yesterday" section: wording, missing-data rules and the rule that a
comparison is made only when it is fair. Pure view-model tests on hand-built payloads (invented
figures); the real-data checks are in tests/integration/test_day_compare.py."""

import datetime as dt
import html as htmllib
import re
from decimal import Decimal as D

import pytest
from dashboard import day_view
from django.template.loader import render_to_string

TODAY, YESTERDAY = dt.date(2026, 10, 19), dt.date(2026, 10, 18)


def _row(key, today, yesterday, *, unit="count", note=""):
    change = None if today is None or yesterday is None else today - yesterday
    direction = "unknown" if change is None else "flat" if change == 0 else (
        "up" if change > 0 else "down")
    return {"key": key, "label": key, "unit": unit, "today": today, "yesterday": yesterday,
            "change": change, "change_pct": None, "direction": direction, "note": note,
            "like_for_like": True, "good_when_up": None}


def payload(*, accounts=(800917, 800917), mtd=(1677, 1565), day=(106, 132),
            out=(D(4000), D(5000)), into=(D(9000), D(7000)), txns=(12, 10), sbi=(D("5147.96"),) * 2,
            est=(None, None), t_state="complete", y_state="complete", like=True, excluded=(),
            scope="network", csp_code=None, sbi_last_sent=dt.date(2026, 10, 14), updated=None,
            excl_known=True):
    return {
        "today": TODAY, "yesterday": YESTERDAY, "scope": scope, "csp_code": csp_code, "csps": 3,
        "rows": [
            _row("accounts", *accounts), _row("new_accounts_mtd", *mtd),
            _row("new_accounts_day", *day), _row("withdrawals", *out, unit="rupees"),
            _row("deposits", *into, unit="rupees"), _row("transactions", *txns),
            _row("observed_avg", *sbi, unit="rupees"),
            _row("estimated_avg", *est, unit="rupees"),
        ],
        "like_for_like_money": like,
        "money_state": {"today": t_state, "yesterday": y_state},
        "accounts_reporting": {"today": 3, "yesterday": 3},
        "excluded": list(excluded), "sbi_last_sent": sbi_last_sent,
        "other_types": {"today": {"txn_count": 4 if excl_known else None},
                        "yesterday": {"txn_count": 3 if excl_known else None}},
        "updated": updated or {"sheet_at": None, "statements_at": None, "statements_day": None},
    }


def page(dc) -> str:
    html = render_to_string("dashboard/components/day_compare.html", {"dc": dc})
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


# ------------------------------------------------------------------ missing is never zero
def test_a_missing_figure_is_a_sentence_never_zero():
    dc = payload(accounts=(None, None), out=(None, 5000), into=(None, 7000),
                 t_state="no_data", like=False)
    text = page(dc)
    assert "Today's report has not arrived" in text
    assert "₹0" not in text and "Waiting for today's report" in text
    assert "—" not in text  # the old ambiguous dash is gone
    v = day_view.friendly(dc)
    assert v["accounts"][0]["today"] is None and v["money"][0]["today"] is None


def test_a_genuine_zero_is_shown_as_zero():
    text = page(payload(out=(D(0), D(0)), t_state="complete", y_state="complete"))
    assert "₹0" in text


# ------------------------------------------------------------------ fair comparisons only
def test_money_is_compared_only_when_both_days_cover_the_same_period():
    fair = day_view.friendly(payload(like=True))
    assert fair["money"][0]["status"]["words"] == "₹1,000 less than yesterday"
    assert fair["money"][1]["status"]["words"] == "₹2,000 more than yesterday"
    unfair = day_view.friendly(payload(like=False, t_state="so_far"))
    for card in unfair["money"]:
        assert card["status"]["kind"] == "waiting"
        assert "than yesterday" not in card["status"]["words"]


def test_partial_days_are_explained_once_for_the_whole_group():
    text = page(payload(like=False, t_state="so_far", y_state="so_far"))
    assert "Today's numbers may change as more information arrives." in text
    assert ("Yesterday's report covers only part of the day, so the totals may not be "
            "directly comparable.") in text
    assert "So far today" in text and "(part of the day)" in text
    v = day_view.friendly(payload(like=False, t_state="so_far", y_state="so_far"))
    assert len(v["notice"]) == 2  # one shared message, not a warning on every card


def test_no_notice_when_both_days_are_complete():
    assert day_view.friendly(payload())["notice"] == []


# ------------------------------------------------------------------ accounts
def test_account_statuses_use_plain_words():
    v = day_view.friendly(payload())
    all_, month, today = v["accounts"]
    assert all_["status"]["words"] == "No change"
    assert month["status"]["words"] == "112 more than yesterday"
    assert today["status"]["words"] == "26 fewer than yesterday"
    assert today["note"] == "Today's number can still grow."
    assert today["status"]["tone"] == ""  # a day still in progress is not coloured as a loss
    assert month["status"]["tone"] == "ok" and month["status"]["icon"] == "up"


def test_big_numbers_use_indian_grouping():
    assert "8,00,917" in page(payload())


# ------------------------------------------------------------------ what is not included
def test_the_excluded_list_is_exactly_what_the_backend_reported():
    dc = payload(excluded=[{"category": "transfer", "today": 4, "yesterday": 2},
                           {"category": "something_new", "today": 1, "yesterday": 0}])
    v = day_view.friendly(dc)
    assert [e["words"] for e in v["excluded"]] == [
        "Money sent from one account to another", "Something new"]
    text = page(dc)
    assert "What is not included?" in text
    assert "Some types of money movement are not included in these totals." in text
    assert "loan" not in text.lower()  # nothing invented


def test_no_exclusion_list_is_invented_when_nothing_was_left_out():
    text = page(payload(excluded=()))
    assert "What is not included?" not in text
    assert "No money movements were left out of these totals." in text
    none_known = page(payload(excluded=(), excl_known=False))
    assert "What is not included?" not in none_known and "left out" not in none_known


# ------------------------------------------------------------------ the two money-per-account cards
def test_no_estimate_says_we_need_more_data_and_links_to_how_it_is_worked_out():
    v = day_view.friendly(payload(est=(None, None)))
    assert v["ours"]["today"] is None
    text = page(payload(est=(None, None)))
    assert "We need more data first" in text
    assert "We need more data before we can make a reliable estimate." in text
    assert "₹0.00" not in text


def test_an_estimate_is_labelled_as_ours_and_links_to_the_explanation_for_one_csp():
    dc = payload(est=(D("5100.50"), D("5090.00")), scope="csp", csp_code="1A850001")
    text = page(dc)
    assert "₹5,100.50" in text and "Our estimate, not an SBI number" in text
    v = day_view.friendly(dc)
    assert v["ours"]["link"].endswith("/csp/1A850001/explain/")
    assert v["ours"]["link_words"] == "How did we calculate this?"


def test_sbi_card_shows_the_real_last_update_date_and_its_explanation():
    text = page(payload(sbi_last_sent=dt.date(2026, 10, 14)))
    assert "₹5,147.96" in text and "Last updated: 14 Oct 2026" in text
    assert "This number changes when SBI sends a new figure." in text
    assert "Last updated" not in page(payload(sbi_last_sent=None))  # never invented


# ------------------------------------------------------------------ what changed?
def test_what_changed_sorts_every_card_into_one_plain_group():
    v = day_view.friendly(payload(est=(None, None)))
    groups = {kind: [i["title"] for i in items] for kind, _l, _i, items in v["changes"]}
    assert "New accounts this month" in groups["up"]
    assert "Accounts opened today" in groups["down"]
    assert "All accounts" in groups["same"]
    assert "Our estimated average" in groups["waiting"]
    assert [label for _k, label, _i, _items in v["changes"]] == [
        "Went up", "Went down", "No change", "Waiting for data"]


# ------------------------------------------------------------------ wording
def test_no_unexplained_jargon_is_shown():
    text = page(payload(like=False, t_state="so_far", y_state="so_far",
                        excluded=[{"category": "transfer", "today": 1, "yesterday": 1}]))
    for word in ("MAB", "provisional", "statement coverage", "carry-forward", "carry forward",
                 "Customer accounts", "Withdrawn by customers", "No comparison yet"):
        assert word not in text, word
    for friendly_label in ("How many accounts are there?", "Money taken out today",
                           "Money put in today", "Accounts opened today",
                           "Average money per account (SBI)",
                           "Our estimated average per account"):
        assert friendly_label in text, friendly_label


def test_update_times_are_real_or_say_nothing_arrived():
    none = page(payload())
    assert "nothing has arrived yet" in none and "no bank report has arrived yet" in none
    import django.utils.timezone as tz
    stale = tz.now() - dt.timedelta(hours=40)
    text = page(payload(updated={"sheet_at": stale, "statements_at": stale,
                                 "statements_day": dt.date(2026, 10, 16)}))
    assert "the source has not refreshed" in text and "covering up to 16 Oct" in text
    assert "When did we last get new information?" in text


def test_why_numbers_are_missing_lists_only_what_applies():
    complete = day_view.friendly(payload(est=(D(5000), D(5000))))["why"]
    assert not any("has not arrived" in w or "part of the day" in w for w in complete)
    assert not any("estimates" in w for w in complete)
    waiting = day_view.friendly(payload(t_state="no_data", like=False, est=(None, None)))["why"]
    assert any("has not arrived yet" in w for w in waiting)
    assert any("our own estimates" in w for w in waiting)
    assert any("straight from SBI" in w for w in waiting)


def test_nothing_is_rendered_without_a_payload():
    assert day_view.friendly(None) is None


@pytest.mark.parametrize("tag", ["Today vs yesterday"])
def test_the_section_keeps_its_title(tag):
    assert tag in page(payload())
