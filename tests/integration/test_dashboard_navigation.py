"""Simplified navigation and the plain-language pages: five primary items, section tabs,
real badge counts, every old route preserved with its permissions, plain wording."""

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
PRIMARY = ["Home", "My CSPs", "Money Movement", "Needs Attention", "More"]


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


def _sidebar(html: str) -> list[str]:
    nav = html.split('<nav class="sidebar-nav"')[1].split("</nav>")[0]
    return [re.sub(r"<[^>]+>|\s+", " ", a).strip() for a in re.findall(r"<a .*?</a>", nav, re.S)]


def _seed_negative_flow():
    Csp.objects.create(csp_code="N", name="Neg CSP", account_count=300)
    DailyBalance.objects.create(
        csp_id="N",
        balance_date=TODAY,
        daily_avg_balance=D(1000),
        account_count=300,
        source="calling_sheet",
    )
    FlowAggregate.objects.create(
        csp_id="N",
        flow_date=TODAY - dt.timedelta(days=2),
        resolution_mode="exact_only",
        txn_count=3,
        credit_amount=D(900),
        debit_amount=D(100),
        recon_status="RECONCILED",
        # customers withdrew 900 and deposited 100: more cash left than came in
        by_category={
            "withdrawal": {"count": 2, "credit": "900", "debit": "0"},
            "deposit": {"count": 1, "credit": "0", "debit": "100"},
        },
    )


# ---------------------------------------------------------------- the five items
def test_sidebar_has_exactly_five_primary_items_for_staff_and_viewer(staff, viewer, client):
    html = client.get("/dashboard/").content.decode()  # `viewer` is the logged-in user here
    assert [re.sub(r"\s*\d+$", "", i) for i in _sidebar(html)] == PRIMARY
    client.logout()
    client.login(username="staff", password="x")
    assert [
        re.sub(r"\s*\d+$", "", i) for i in _sidebar(client.get("/dashboard/").content.decode())
    ] == PRIMARY


def test_active_item_follows_the_page_and_old_pages_keep_their_section(staff):
    cases = {
        "/dashboard/": "Home",
        "/dashboard/money/": "Money Movement",
        "/dashboard/needs-attention/": "Needs Attention",
        "/dashboard/more/": "More",
        "/dashboard/bank-flow/": "Money Movement",
    }
    for url, label in cases.items():
        html = staff.get(url).content.decode()
        nav = html.split('<nav class="sidebar-nav"')[1].split("</nav>")[0]
        active = re.findall(r'<a [^>]*class="active"[^>]*>(.*?)</a>', nav, re.S)
        assert len(active) == 1 and label in re.sub(r"<[^>]+>|\s+", " ", active[0]), url
        assert 'aria-current="page"' in nav


def test_section_tabs_group_sibling_pages_and_respect_permissions(staff):
    staff_tabs = [t["label"] for t in navigation.build("transactions", True)["nav_tabs"]]
    viewer_tabs = [t["label"] for t in navigation.build("transactions", False)["nav_tabs"]]
    assert staff_tabs == [
        "Overview",
        "Data progress",
        "Transactions",
        "Daily Comparison",
        "Trends & Analytics",
        "Bank-Flow Validation",
    ]
    assert "Bank-Flow Validation" not in viewer_tabs and "Transactions" in viewer_tabs
    csps = navigation.build("csp_detail", False)["nav_tabs"]
    assert [t["label"] for t in csps] == ["CSP Directory", "CSP 360°", "Balance Intelligence"]
    assert [t["label"] for t in csps if t["active"]] == ["CSP 360°"]  # only one lights up
    att = [t["label"] for t in navigation.build("needs_attention", True)["nav_tabs"]]
    assert att == ["All items", "Risk", "Actions", "Approvals", "Verification", "Agent Findings"]
    assert [t["label"] for t in navigation.build("needs_attention", False)["nav_tabs"]] == [
        "All items"
    ]
    html = staff.get("/dashboard/bank-flow/").content.decode()
    assert 'class="subnav"' in html and "Trends &amp; Analytics" in html


def test_every_dashboard_page_belongs_to_a_section():
    skip = {"login", "logout"}
    names = {p.name for p in urlpatterns if p.name and p.name not in skip}
    assert not names - set(navigation.SECTION_OF), names - set(navigation.SECTION_OF)


def test_more_hub_lists_every_secondary_tool_and_hides_staff_tools_from_viewers(
    staff, viewer, client
):
    body = client.get("/dashboard/more/").content.decode()
    for ok in ("Classic overview", "Balance Intelligence", "Trends &amp; Analytics"):
        assert ok in body
    for hidden in (
        "Messaging",
        "Administration",
        "API Documentation",
        "System Health",
        "Data Pipeline",
        "Audit",
        "AI Operations",
        "Agent Monitoring",
    ):
        assert f"<strong>{hidden}</strong>" not in body, hidden
    client.logout()
    client.login(username="staff", password="x")
    body = client.get("/dashboard/more/").content.decode()
    for name in (
        "Messaging",
        "Administration",
        "API Documentation",
        "System Health",
        "Data Pipeline",
        "Audit",
        "AI Operations",
        "AI Recommendations",
        "Agent Monitoring",
        "Bank-Flow Validation",
        "Classic overview",
    ):
        assert f"<strong>{name}</strong>" in body, name
    # every linked destination still exists
    for href in re.findall(r'<li><a href="([^"]+)"', body):
        assert href.startswith("/dashboard/")


def test_old_routes_are_all_still_registered_and_staff_pages_stay_protected(staff, viewer, client):
    old = [
        "home",
        "balance_intelligence",
        "trends",
        "csp_comparisons",
        "csp_directory",
        "transactions",
        "bank_flow",
        "api_docs",
        "admin_embed",
        "system_health",
        "pipeline",
        "messaging",
        "ai_operations",
        "agent_monitoring",
        "risk",
        "agent_findings",
        "verification",
        "actions",
        "approvals",
        "ai_recommendations",
        "audit",
    ]
    for n in old:
        assert reverse(f"dashboard:{n}")
    assert reverse("dashboard:csp_detail", kwargs={"csp_code": "A"}) == "/dashboard/csp/A/"
    assert client.get("/dashboard/bank-flow/").status_code == 403  # viewer: still blocked
    client.logout()
    client.login(username="staff", password="x")
    assert client.get("/dashboard/bank-flow/").status_code == 200
    client.logout()
    for url in (
        "/dashboard/",
        "/dashboard/money/",
        "/dashboard/needs-attention/",
        "/dashboard/more/",
    ):
        assert client.get(url).status_code == 302  # login still required


# ---------------------------------------------------------------- badge
def test_badge_is_hidden_without_real_items_and_shows_the_real_count(staff):
    html = staff.get("/dashboard/more/").content.decode()
    assert re.search(r'class="nav-badge"[^>]*id="nav-attention-badge"[^>]* hidden>', html)
    assert staff.get("/dashboard/shell/").json()["attention_count"] is None
    _seed_negative_flow()
    from common.cache import bump_generation

    bump_generation()
    assert staff.get("/dashboard/shell/").json()["attention_count"] == 1
    html = staff.get("/dashboard/more/").content.decode()  # now cached: inline, not hidden
    tag = re.search(r'<span class="nav-badge"[^>]*id="nav-attention-badge"[^>]*>1<', html)
    assert tag and " hidden" not in tag.group(0)


# ---------------------------------------------------------------- wording
JARGON = (
    "INSUFFICIENT_EVIDENCE",
    "PMJDY",
    "pool flow",
    "Net pool",
    "KO code",
    "exact_only",
    "carry_forward",
    "exception",
    "Avg Balance",
    "reconcil",
    "unmatched CSP",
)


def test_home_is_plain_english_outside_advanced_details(staff):
    _seed_negative_flow()
    html = staff.get("/dashboard/").content.decode()
    visible = html.split('<details class="sm-advanced"')[0]
    visible = visible.split("<main", 1)[1]
    for term in JARGON:
        assert term not in visible, term
    for phrase in (
        "Customer accounts",
        "Withdrawn by customers",
        "Deposited by customers",
        "SBI-reported average balance",
        "Reliable estimate not available yet",
        "Today in short",
        "Needs attention",
        "Your CSPs",
        "Deposits minus withdrawals",
    ):
        assert phrase in visible, phrase
    assert "Advanced details" in html and "How reliable are our estimates?" in html


def test_unvalidated_estimate_is_never_shown_as_a_number_on_the_home_cards(staff):
    _seed_negative_flow()
    estimates.compute_daily_estimates(TODAY)
    html = staff.get("/dashboard/").content.decode()
    cards = html.split('<section class="sm-cards"')[1].split("</section>")[0]
    assert "Our estimated average balance" not in cards
    assert (
        "SBI-reported average balance" in cards and "Reliable estimate not available yet" in cards
    )
    assert "Not available yet" in html  # the table says so per CSP


def test_validated_estimate_is_shown_with_its_status_and_the_sbi_figure_beside_it(staff):
    _seed_negative_flow()
    HypothesisRun.objects.create(
        status="VALIDATED", detail="ok", selected_method="growth_only", n_pairs=40, n_events=10
    )
    DailyEstimate.objects.create(
        csp_id="N",
        estimate_date=TODAY,
        method="growth_only",
        status="VALIDATED",
        reason_code="GROWTH_ONLY_VALIDATED",
        baseline_value=D(1000),
        estimated_value=D(1030),
        estimated_delta=D(30),
        baseline_account_count=300,
        current_account_count=309,
        calculated_at=timezone.now(),
    )
    html = staff.get("/dashboard/").content.decode()
    cards = html.split('<section class="sm-cards"')[1].split("</section>")[0]
    assert "Our estimated average balance" in cards and "₹1,030" in cards
    assert "Checked against past data" in cards and "SBI-reported: ₹1,000" in cards


def test_provisional_estimate_is_labelled_as_still_being_checked(staff):
    _seed_negative_flow()
    HypothesisRun.objects.create(
        status="PROVISIONAL", detail="early", selected_method="growth_only"
    )
    DailyEstimate.objects.create(
        csp_id="N",
        estimate_date=TODAY,
        method="growth_only",
        status="PROVISIONAL",
        reason_code="x",
        baseline_value=D(1000),
        estimated_value=D(1010),
        estimated_delta=D(10),
        current_account_count=300,
        calculated_at=timezone.now(),
    )
    cards = staff.get("/dashboard/").content.decode().split('<section class="sm-cards"')[1]
    assert "Early estimate, still being checked" in cards


def test_stale_and_incomplete_data_are_labelled_in_plain_words(staff):
    from bankflow.models import PmjdyMonthly

    _seed_negative_flow()
    html = staff.get("/dashboard/").content.decode()
    assert "New customer accounts for" in html and "have not been captured yet" in html
    assert "Money movement covers 1 of the last 30 days; totals are incomplete." in html
    old = timezone.now() - dt.timedelta(days=4)
    PmjdyMonthly.objects.create(
        csp_id="N",
        month=f"{TODAY:%Y-%m}",
        mtd=2,
        ftd=0,
        target=7,
        observed_at=old,
        snapshot_date=timezone.localtime(old).date(),
    )
    assert "were last updated" in staff.get("/dashboard/?range=7").content.decode()


def test_summary_sentences_use_only_real_numbers():
    s = {
        "accounts": {"current_total": 3340, "growth": 90, "by_source": {"pmjdy": 2}},
        "new_accounts": {
            "available": False,
            "mtd": None,
            "today": None,
            "month_label": "October 2026",
        },
        "flows": {
            "days_with_data": 5,
            "withdrawals": D(1000000),
            "deposits": D(400000),
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
    s["new_accounts"] = {
        "available": True,
        "mtd": 1246,
        "today": 130,
        "month_label": "October 2026",
    }
    assert plain.daily_summary(s, 0, 0, 30)[0] == (
        "There are 3,340 customer accounts. 1,246 new customer accounts were opened so far in "
        "October 2026 (130 on the latest day)."
    )
    s["flows"]["days_with_data"] = 0
    s["accounts"]["growth"] = None
    s["new_accounts"] = {
        "available": False,
        "mtd": None,
        "today": None,
        "month_label": "October 2026",
    }
    again = plain.daily_summary(s, 0, 0, 7)
    assert "No bank statements cover this period yet" in again[1]
    assert "We do not have an earlier count yet" in again[0] and again[-1].startswith(
        "Nothing needs"
    )


def test_friendly_exceptions_never_claim_a_cause_or_use_codes():
    for title, meaning, action in plain.FRIENDLY.values():
        assert title and meaning and action and "_" not in title
        assert "because" not in meaning.lower()


# ---------------------------------------------------------------- the new pages
def test_needs_attention_page_filters_paginates_and_links_to_the_csp(staff, viewer, client):
    _seed_negative_flow()
    page = client.get("/dashboard/needs-attention/").content.decode()  # viewer allowed
    assert "Customers withdrew much more than they deposited" in page
    assert "/dashboard/csp/N/" in page
    assert "What to do:" in page and "prompts to look, not conclusions" in page
    assert "NEGATIVE_NET_FLOW" not in page.split("<main", 1)[1].split("<option", 1)[0]
    filtered = client.get("/dashboard/needs-attention/?type=NEGATIVE_NET_FLOW&page=9&range=7")
    assert filtered.status_code == 200
    assert client.get("/dashboard/needs-attention/?type=bogus&range=abc").status_code == 200


def test_money_movement_page_shows_day_rows_and_marks_missing_days(staff):
    _seed_negative_flow()
    html = staff.get("/dashboard/money/?range=7").content.decode()
    assert "Withdrawn by customers" in html and "Deposits minus withdrawals" in html
    assert "No bank statement for this day" in html and "May be incomplete" in html
    assert "−₹800" in html or "−₹800.00" in html or "800" in html


def test_home_table_is_searchable_filterable_and_safe_with_bad_input(staff):
    _seed_negative_flow()
    Csp.objects.create(csp_code="OK1", name="Quiet CSP", account_count=300)
    DailyBalance.objects.create(
        csp_id="OK1",
        balance_date=TODAY,
        daily_avg_balance=D(900),
        account_count=300,
        source="calling_sheet",
    )
    html = staff.get("/dashboard/?q=quiet").content.decode()
    assert "Quiet CSP" in html and "Neg CSP" not in html.split('id="my-csps"')[1]
    only = staff.get("/dashboard/?show=attention").content.decode().split('id="my-csps"')[1]
    assert "Neg CSP" in only and "Quiet CSP" not in only
    assert staff.get("/dashboard/?range=x&sort=drop&show=zzz&page=-3&order=up").status_code == 200


def test_responsive_and_theme_guards():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "dashboard"
    css = (root / "static/dashboard/dashboard.css").read_text(encoding="utf-8")
    base = (root / "templates/dashboard/base.html").read_text(encoding="utf-8")
    assert (
        'data-theme="{% if is_eko_mode %}eko{% else %}light{% endif %}"' in base
    )  # soft light default
    assert "@media (max-width:700px)" in css and "attr(data-label)" in css  # stacked table rows
    assert ".sidebar.mobile-open" in css and 'id="sidebar-mobile-toggle"' in base  # drawer
    ui = (root / "static/dashboard/ui-kit.js").read_text(encoding="utf-8")
    assert (
        'collapsed = localStorage.getItem("sidebar-collapsed") === "1"' in ui
        or 'getItem("sidebar-collapsed") === "1"' in ui
    )  # expanded unless the user collapsed it
