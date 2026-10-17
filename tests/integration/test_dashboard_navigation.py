"""Navigation and plain-language pages: the sidebar follows the journey, section tabs, a real
review count, every old route preserved with its permissions, honest estimate labels."""

import datetime as dt
import decimal
import re

import pytest
from bankflow import estimates
from bankflow.models import DailyEstimate, FlowAggregate, HypothesisRun
from csp.models import Csp, DailyBalance
from dashboard import navigation, plain
from dashboard.urls import urlpatterns
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

D = decimal.Decimal
pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
STAFF_NAV = ["Overview", "Data readiness", "CSP balances", "Money movement", "Review",
             "SBI comparison", "Month-end outlook", "Operations"]
VIEWER_NAV = [n for n in STAFF_NAV if n != "SBI comparison"]


@pytest.fixture(autouse=True)
def _permissive_alert_thresholds(settings):
    """These tests exercise the alert plumbing with tiny fixture amounts; the real materiality
    thresholds are tested in test_alert_materiality.py."""
    settings.ALERT_MIN_OUTFLOW = 0
    settings.ALERT_MIN_IMBALANCE = 0
    settings.ALERT_HIGH_OUTFLOW = 0
    settings.ALERT_HIGH_IMBALANCE = 0
    settings.ALERT_MIN_NETWORK_COVERAGE = 0


@pytest.fixture
def staff(client):
    get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.login(username="staff", password="x")
    return client


@pytest.fixture
def viewer(client):
    get_user_model().objects.create_user("viewer", password="x", is_staff=False)
    client.login(username="viewer", password="x")
    return client


def _nav(html: str) -> str:
    return html.split('<nav class="nav"')[1].split("</nav>")[0]


def _sidebar(html: str) -> list[str]:
    """The primary navigation items, with any count badge stripped."""
    items = re.findall(r'<a class="item".*?</a>', _nav(html), re.S)
    return [re.sub(r"\s*\d+$", "", re.sub(r"<[^>]+>|\s+", " ", a).strip()) for a in items]


def _seed_negative_flow():
    Csp.objects.create(csp_code="N", name="Neg CSP", account_count=300)
    DailyBalance.objects.create(
        csp_id="N", balance_date=TODAY, daily_avg_balance=D(1000), account_count=300,
        source="calling_sheet")
    FlowAggregate.objects.create(
        csp_id="N", flow_date=TODAY - dt.timedelta(days=2), resolution_mode="exact_only",
        txn_count=3, credit_amount=D(900), debit_amount=D(100), recon_status="RECONCILED",
        # customers withdrew 900 and deposited 100: more cash left than came in
        by_category={
            "withdrawal": {"count": 2, "credit": "900", "debit": "0"},
            "deposit": {"count": 1, "credit": "0", "debit": "100"},
        })


# ---------------------------------------------------------------- the sidebar
def test_sidebar_follows_the_journey_and_hides_staff_items_from_viewers(staff, viewer, client):
    html = client.get("/dashboard/").content.decode()  # `viewer` is the logged-in user here
    assert _sidebar(html) == VIEWER_NAV
    client.logout()
    client.login(username="staff", password="x")
    assert _sidebar(client.get("/dashboard/").content.decode()) == STAFF_NAV


def test_active_item_follows_the_page_and_sibling_pages_keep_their_section(staff):
    cases = {
        "/dashboard/": "Overview",
        "/dashboard/readiness/": "Data readiness",
        "/dashboard/csps/": "CSP balances",
        "/dashboard/comparisons/": "CSP balances",
        "/dashboard/money/": "Money movement",
        "/dashboard/transactions/": "Money movement",
        "/dashboard/needs-attention/": "Review",
        "/dashboard/risk/": "Review",
        "/dashboard/sbi-comparison/": "SBI comparison",
        "/dashboard/month-end/": "Month-end outlook",
        "/dashboard/more/": "Operations",
        "/dashboard/system-health/": "Operations",
    }
    for url, label in cases.items():
        nav = _nav(staff.get(url).content.decode())
        active = re.findall(r'<a class="item"[^>]*aria-current="page"[^>]*>(.*?)</a>', nav, re.S)
        assert len(active) == 1 and label in re.sub(r"<[^>]+>|\s+", " ", active[0]), url


def test_section_tabs_group_sibling_pages_and_respect_permissions(staff):
    assert [t["label"] for t in navigation.build("transactions", True)["nav_tabs"]] == [
        "Summary", "Transaction workbook", "Trends"]
    csps = navigation.build("csp_detail", False)["nav_tabs"]
    assert [t["label"] for t in csps] == ["All CSPs", "Day by day", "Incentive slabs"]
    assert [t["label"] for t in csps if t["active"]] == ["All CSPs"]  # only one lights up
    review = [t["label"] for t in navigation.build("needs_attention", True)["nav_tabs"]]
    assert review == ["Data to review", "Risk", "Assistant findings", "Verification",
                      "Proposed actions", "Approvals"]
    assert [t["label"] for t in navigation.build("needs_attention", False)["nav_tabs"]] == [
        "Data to review"]
    html = staff.get("/dashboard/trends/").content.decode()
    assert 'class="tabs"' in html and "Transaction workbook" in html


def test_every_dashboard_page_belongs_to_a_section():
    skip = {"login", "logout", "bank_flow_legacy"}
    names = {p.name for p in urlpatterns if p.name and p.name not in skip}
    assert not names - set(navigation.SECTION_OF), names - set(navigation.SECTION_OF)


def test_operations_hub_lists_every_tool_and_hides_staff_tools_from_viewers(staff, viewer, client):
    body = client.get("/dashboard/more/").content.decode()
    for ok in ("Data readiness", "Incentive slabs"):
        assert ok in body
    for hidden in ("Draft messages", "Administration", "API documentation", "System health",
                   "Live pipeline", "Audit trail", "AI operations", "Assistant monitoring"):
        assert f"<b>{hidden}</b>" not in body, hidden
    assert "read-only access" in body
    client.logout()
    client.login(username="staff", password="x")
    body = client.get("/dashboard/more/").content.decode()
    for name in ("Draft messages", "Administration", "API documentation", "System health",
                 "Live pipeline", "Audit trail", "AI operations", "AI recommendations",
                 "Assistant monitoring", "Data readiness", "Incentive slabs"):
        assert f"<b>{name}</b>" in body, name
    for href in re.findall(r'<td class="name"><a href="([^"]+)"', body):
        assert href.startswith("/dashboard/")


def test_routes_exist_staff_pages_stay_protected_and_old_links_redirect(staff, viewer, client):
    for n in ("home", "data_readiness", "csp_directory", "csp_comparisons", "balance_intelligence",
              "money_movement", "transactions", "trends", "needs_attention", "bank_flow",
              "month_end", "more", "system_health", "pipeline", "audit", "messaging",
              "ai_operations", "agent_monitoring", "risk", "agent_findings", "verification",
              "actions", "approvals", "ai_recommendations", "api_docs", "admin_embed",
              "overview_classic", "data_progress"):
        assert reverse(f"dashboard:{n}")
    assert reverse("dashboard:csp_detail", kwargs={"csp_code": "A"}) == "/dashboard/csp/A/"
    assert reverse("dashboard:csp_explain", kwargs={"csp_code": "A"}) == "/dashboard/csp/A/explain/"
    for url in ("/dashboard/sbi-comparison/", "/dashboard/bank-flow/"):
        assert client.get(url, follow=True).status_code == 403  # viewer: still blocked
    client.logout()
    client.login(username="staff", password="x")
    assert client.get("/dashboard/sbi-comparison/").status_code == 200
    client.logout()
    for url in ("/dashboard/", "/dashboard/money/", "/dashboard/needs-attention/",
                "/dashboard/more/", "/dashboard/readiness/", "/dashboard/month-end/"):
        assert client.get(url).status_code == 302  # login still required


# ---------------------------------------------------------------- the review count
def test_the_sidebar_count_is_the_same_number_the_review_page_opens_with(staff):
    from bankflow import intelligence, review
    from common.cache import bump_generation

    _seed_negative_flow()
    bump_generation()
    frm, to = intelligence.resolve_range(30)
    items = intelligence.exceptions(frm, to, limit=100000)["items"]
    alerts = sum(1 for i in items if i["csp_code"] and i["severity"] in ("high", "medium"))
    expected = alerts + review.review_queue(30)["open"]
    assert alerts == 1 and expected >= 1
    assert staff.get("/dashboard/shell/").json()["attention_count"] == expected
    html = staff.get("/dashboard/more/").content.decode()  # now cached: inline, not hidden
    tag = re.search(r'<span class="count" id="review-count"[^>]*>(\d+)<', html)
    assert tag and int(tag.group(1)) == expected and " hidden" not in tag.group(0)


# ---------------------------------------------------------------- wording
JARGON = (
    "INSUFFICIENT_EVIDENCE", "PMJDY", "pool flow", "Net pool", "KO code", "exact_only",
    "carry_forward", "exception", "Avg Balance", "reconcil", "unmatched CSP",
)


def test_overview_and_review_are_plain_english(staff):
    _seed_negative_flow()
    for url in ("/dashboard/", "/dashboard/needs-attention/"):
        html = staff.get(url).content.decode()
        visible = html.split("<main", 1)[1].split("<option", 1)[0].split("<script", 1)[0]
        for term in JARGON:
            assert term not in visible, (url, term)
    overview = staff.get("/dashboard/").content.decode()
    for phrase in ("CSPs in scope", "Valid SBI baselines", "Estimates", "Data freshness",
                   "Unlinked statement rows", "Large balance movements",
                   "Missing or stale account counts", "Month-to-date estimate",
                   "Projected month-end average", "Needs attention"):
        assert phrase in overview, phrase


def test_an_unvalidated_estimate_is_labelled_provisional_never_reliable(staff):
    _seed_negative_flow()
    estimates.compute_daily_estimates(TODAY)
    html = staff.get("/dashboard/csps/").content.decode()
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    assert "Provisional" in body and "Carry-forward, not validated" in body
    assert "Reliable" not in body
    detail = staff.get("/dashboard/csp/N/").content.decode()
    assert "Our estimate" in detail and "not a confirmed balance" in detail


def test_a_validated_estimate_is_labelled_reliable_beside_the_sbi_value(staff):
    _seed_negative_flow()
    HypothesisRun.objects.create(
        status="VALIDATED", detail="ok", selected_method="growth_only", n_pairs=40, n_events=10)
    DailyEstimate.objects.create(
        csp_id="N", estimate_date=TODAY, method="growth_only", status="VALIDATED",
        reason_code="GROWTH_ONLY_VALIDATED", baseline_value=D(1000), baseline_date=TODAY,
        last_seen_date=TODAY, estimated_value=D(1030), estimated_delta=D(30),
        baseline_account_count=300, current_account_count=309, calculated_at=timezone.now())
    html = staff.get("/dashboard/csps/").content.decode()
    row = html.split("<tbody>")[1].split("</tbody>")[0]
    assert "Reliable" in row and "₹1,030" in row and "₹1,000" in row


def test_stale_and_incomplete_data_are_labelled_in_plain_words(staff):
    _seed_negative_flow()
    html = staff.get("/dashboard/readiness/").content.decode()
    assert "Did the imports work?" in html
    assert "Is the data ready for reliable estimates?" in html and "Provisional" in html
    review_html = staff.get("/dashboard/needs-attention/").content.decode()
    assert "Statement coverage gaps" in review_html
    assert "No bank statement covers this day" in review_html


def test_summary_sentences_use_only_real_numbers():
    s = {
        "accounts": {"current_total": 3340, "growth": 90, "by_source": {"pmjdy": 2}},
        "new_accounts": {
            "available": False, "mtd": None, "today": None, "month_label": "October 2026",
        },
        "flows": {
            "days_with_data": 5, "withdrawals": D(1000000), "deposits": D(400000),
            "net_customer_flow": D(-600000),
        },
        "estimates": {"network_estimate": None},
    }
    lines = plain.daily_summary(s, attention_total=2, high=1, days=30)
    assert lines[0] == "There are 3,340 customer accounts, 90 new in the last 30 days."
    assert "Customers withdrew ₹10,00,000 and deposited ₹4,00,000" in lines[1]
    assert "₹6,00,000 more was withdrawn than deposited" in lines[1]
    assert lines[2].startswith("Reliable estimate not available yet")
    assert lines[3] == "2 items need attention, 1 to check today."
    s["new_accounts"] = {"available": True, "mtd": 1246, "today": 130,
                         "month_label": "October 2026"}
    assert plain.daily_summary(s, 0, 0, 30)[0] == (
        "There are 3,340 customer accounts. 1,246 new customer accounts were opened so far in "
        "October 2026 (130 on the latest day)."
    )
    s["flows"]["days_with_data"] = 0
    s["accounts"]["growth"] = None
    s["new_accounts"] = {"available": False, "mtd": None, "today": None,
                         "month_label": "October 2026"}
    again = plain.daily_summary(s, 0, 0, 7)
    assert "No bank statements cover this period yet" in again[1]
    assert "We do not have an earlier count yet" in again[0]
    assert again[-1].startswith("Nothing needs")


def test_friendly_exceptions_never_claim_a_cause_or_use_codes():
    for title, meaning, action in plain.FRIENDLY.values():
        assert title and meaning and action and "_" not in title
        assert "because" not in meaning.lower()


# ---------------------------------------------------------------- the pages
def test_review_page_filters_paginates_and_links_to_the_csp(staff, viewer, client):
    _seed_negative_flow()
    page = client.get("/dashboard/needs-attention/").content.decode()  # viewer allowed
    assert "Customers withdrew much more than they deposited" in page
    assert "/dashboard/csp/N/" in page
    assert "What to do" in page and "reasons to look, not proof of a problem" in page
    assert "NEGATIVE_NET_FLOW" not in page.split("<main", 1)[1].split("<option", 1)[0]
    filtered = client.get("/dashboard/needs-attention/?type=NEGATIVE_NET_FLOW&page=9&range=7")
    assert filtered.status_code == 200
    assert client.get("/dashboard/needs-attention/?type=bogus&range=abc&c=zzz").status_code == 200


def test_money_movement_page_shows_day_rows_and_marks_missing_days(staff):
    _seed_negative_flow()
    html = staff.get("/dashboard/money/?range=7").content.decode()
    assert "Withdrawn by customers" in html and "Deposits minus withdrawals" in html
    assert "No bank statement for this day" in html and "May be incomplete" in html
    assert "−₹800" in html


def test_csp_list_is_searchable_filterable_and_safe_with_bad_input(staff):
    _seed_negative_flow()
    Csp.objects.create(csp_code="OK1", name="Quiet CSP", account_count=300)
    DailyBalance.objects.create(
        csp_id="OK1", balance_date=TODAY, daily_avg_balance=D(900), account_count=300,
        source="calling_sheet")
    found = staff.get("/dashboard/csps/?q=quiet").content.decode()
    body = found.split("<tbody>")[1].split("</tbody>")[0]
    assert "Quiet CSP" in body and "Neg CSP" not in body
    only = staff.get("/dashboard/csps/?show=review").content.decode().split("<tbody>")[1]
    assert "Neg CSP" in only and "Quiet CSP" not in only.split("</tbody>")[0]
    for q in ("?show=zzz&sort=drop&page=-3&order=up", "?view=day&sort=zzz&page=x&order=up",
              "?q=" + "x" * 500):
        assert staff.get("/dashboard/csps/" + q).status_code == 200


def test_responsive_and_theme_guards():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "dashboard"
    css = (root / "static/dashboard/app.css").read_text(encoding="utf-8")
    base = (root / "templates/dashboard/base.html").read_text(encoding="utf-8")
    assert 'data-theme="light"' in base  # light by default
    assert "@media (max-width: 860px)" in css and "prefers-reduced-motion" in css
    assert 'id="menu-btn"' in base and 'aria-controls="nav"' in base and 'id="scrim"' in base
    assert ":focus-visible" in css and 'class="skip"' in base  # keyboard access
    assert "fonts.googleapis.com" not in base  # no third-party font request
