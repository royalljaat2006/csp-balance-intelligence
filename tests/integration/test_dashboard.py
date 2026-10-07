"""
Dashboard views: login-gating, and that real seeded data actually renders
(not just returns 200) — complements the manual verification against the
live CALLING SHEET + real transaction data documented in docs/DATA-FLOW.md.
"""

import datetime as dt
import decimal

import pytest
from csp.models import Csp, MonthlySummary
from django.contrib.auth import get_user_model
from django.test import override_settings


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(username="staff", password="pw12345", is_staff=True)


@pytest.fixture
def logged_in_client(client, staff_user):
    client.login(username="staff", password="pw12345")
    return client


@pytest.fixture
def viewer_user(db):
    """A non-staff account — read-only monitoring access (Overview/Trends/
    CSP detail), no API docs or Admin console. See dashboard/views.py."""
    return get_user_model().objects.create_user(
        username="viewer", password="pw12345", is_staff=False
    )


@pytest.fixture
def viewer_client(client, viewer_user):
    client.login(username="viewer", password="pw12345")
    return client


@pytest.fixture
def csp_with_summary(db):
    csp = Csp.objects.create(csp_code="1A850001", name="Test CSP", account_count=300)
    month = dt.date.today().strftime("%Y-%m")
    MonthlySummary.objects.create(
        csp=csp,
        month=month,
        days_in_month=30,
        days_with_data=10,
        mtd_mab=decimal.Decimal("3000.00"),
        slab=MonthlySummary.Slab.S1,
        incentive_rate_pa=decimal.Decimal("1.10"),
        gap_to_min=decimal.Decimal("0"),
        gap_to_next_slab=decimal.Decimal("1000.00"),
        trend_flag=MonthlySummary.Trend.STABLE,
        is_eligible=True,
    )
    return csp


@pytest.mark.django_db
def test_home_requires_login(client):
    response = client.get("/dashboard/")
    assert response.status_code == 302
    assert "/dashboard/login/" in response.url


@pytest.mark.django_db
def test_root_redirects_to_dashboard(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == "/dashboard/"


@pytest.mark.django_db
def test_home_renders_with_real_seeded_data(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Test CSP" in body
    assert "1A850001" in body
    assert '"slab": "S1"' in body  # embedded via json_script for the table


@pytest.mark.django_db
def test_home_never_leaks_an_api_key(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    assert "X-API-Key" not in response.content.decode()


@pytest.mark.django_db
def test_home_shows_today_vs_yesterday_kpis(logged_in_client, csp_with_summary):
    from csp.models import DailyBalance

    today = dt.date.today()
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=today - dt.timedelta(days=1),
        daily_avg_balance=decimal.Decimal("3000"), source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=today,
        daily_avg_balance=decimal.Decimal("3500"), source=DailyBalance.Source.CALLING_SHEET,
    )

    response = logged_in_client.get("/dashboard/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Today vs. yesterday" in body
    assert response.context["reporting_today"] == 1
    assert response.context["daily_trend_counts"]["GROWTH"] == 1
    assert len(response.context["top_growth_today"]) == 1
    assert response.context["top_growth_today"][0]["csp_code"] == csp_with_summary.csp_code


@pytest.mark.django_db
def test_home_today_vs_yesterday_kpis_with_no_balance_data(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    assert response.status_code == 200
    assert response.context["reporting_today"] == 0
    assert response.context["daily_trend_counts"]["NO_DATA"] == 1


@pytest.mark.django_db
def test_home_shows_ai_briefing_placeholder_without_insight(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    body = response.content.decode()
    assert "AI briefing" in body
    assert "No AI briefing generated yet" in body


@pytest.mark.django_db
def test_home_shows_latest_insight_and_priority_calls(logged_in_client, csp_with_summary):
    from autopilot.models import Insight, PriorityCall

    month = csp_with_summary.monthly_summaries.first().month
    Insight.objects.create(
        month=month, headline="Balances holding steady", body="Nothing notable this week.",
        model_used="fake-model",
    )
    PriorityCall.objects.create(
        month=month, csp=csp_with_summary, rank=1, reason="Small gap, declining trend.",
        signals_used={"slab": "S1", "gap": 1000.0},
    )

    response = logged_in_client.get("/dashboard/")
    body = response.content.decode()
    assert "Balances holding steady" in body
    assert "Nothing notable this week." in body
    assert "AI priority calls" in body
    assert "Small gap, declining trend." in body


@pytest.mark.django_db
def test_home_shows_unreviewed_anomaly_count(logged_in_client, csp_with_summary):
    from autopilot.models import AnomalyFlag
    from csp.models import IngestLog

    log = IngestLog.objects.create(source="calling_sheet")
    AnomalyFlag.objects.create(ingest_log=log, description="Odd jump", severity="high")

    response = logged_in_client.get("/dashboard/")
    assert "1 data anomaly flagged" in response.content.decode()


@pytest.mark.django_db
def test_home_shows_per_csp_account_growth(logged_in_client, csp_with_summary):
    from csp.models import DailyBalance

    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 1),
        daily_avg_balance=decimal.Decimal("3000"), account_count=288,
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 2),
        daily_avg_balance=decimal.Decimal("3000"), account_count=300,
        source=DailyBalance.Source.CALLING_SHEET,
    )

    response = logged_in_client.get("/dashboard/")
    body = response.content.decode()
    assert 'data-key="net_new_accounts"' in body  # sortable table header
    assert '"net_new_accounts": 12' in body  # embedded via json_script


@pytest.mark.django_db
def test_home_account_growth_null_without_history(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    assert '"net_new_accounts": null' in response.content.decode()


@pytest.mark.django_db
def test_csp_detail_renders(logged_in_client, csp_with_summary):
    response = logged_in_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/")
    assert response.status_code == 200
    assert "Test CSP" in response.content.decode()


@pytest.mark.django_db
def test_csp_detail_unknown_code_404s(logged_in_client, db):
    response = logged_in_client.get("/dashboard/csp/DOES-NOT-EXIST/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_csp_detail_shows_daily_comparison_growth(logged_in_client, csp_with_summary):
    from csp.models import DailyBalance

    today = dt.date.today()
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=today - dt.timedelta(days=1),
        daily_avg_balance=decimal.Decimal("3000"), source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=today,
        daily_avg_balance=decimal.Decimal("3300"), source=DailyBalance.Source.CALLING_SHEET,
    )

    response = logged_in_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Daily comparison" in body
    assert "growth" in body.lower()


@pytest.mark.django_db
def test_csp_detail_daily_comparison_shows_no_data_without_balances(
    logged_in_client, csp_with_summary
):
    response = logged_in_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Daily comparison" in body
    assert "no data" in body.lower()


@pytest.mark.django_db
def test_csp_detail_account_growth_placeholder_without_data(logged_in_client, csp_with_summary):
    response = logged_in_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/")
    body = response.content.decode()
    assert "Account growth" in body
    assert "Only 0 daily account-count reading recorded so far" in body


@pytest.mark.django_db
def test_csp_detail_shows_account_growth_when_available(logged_in_client, csp_with_summary):
    from csp.models import DailyBalance

    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 1),
        daily_avg_balance=decimal.Decimal("3000"), account_count=100,
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 2),
        daily_avg_balance=decimal.Decimal("3000"), account_count=112,
        source=DailyBalance.Source.CALLING_SHEET,
    )

    response = logged_in_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/")
    body = response.content.decode()
    assert "account-growth-chart" in body
    assert "+12" in body  # net-new hint in the panel header
    assert "'value': 112" in body


@pytest.mark.django_db
def test_trends_page_renders_without_activity_data(logged_in_client, db):
    response = logged_in_client.get("/dashboard/trends/")
    assert response.status_code == 200
    assert "No transaction activity has been ingested yet" in response.content.decode()
    # No mart yet -> no bounds -> the date-range picker doesn't render at all.
    assert "date-range-form" not in response.content.decode()


def _create_daily_activity_mart():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            create table if not exists daily_activity (
                csp_code varchar(32), activity_date date, txn_count int,
                txn_amount numeric, withdrawal_count int, withdrawal_amount numeric,
                deposit_count int, deposit_amount numeric, cash_in_pool numeric,
                cash_out_pool numeric, net_flow numeric,
                onus_txn_count int, onus_txn_amount numeric,
                onus_withdrawal_count int, onus_withdrawal_amount numeric,
                onus_deposit_count int, onus_deposit_amount numeric,
                onus_cash_in_pool numeric, onus_cash_out_pool numeric, onus_net_flow numeric
            )
            """
        )
        cursor.execute("delete from daily_activity")


@pytest.mark.django_db
def test_trends_page_shows_today_vs_yesterday_transaction_comparison(logged_in_client, db):
    from django.db import connection

    _create_daily_activity_mart()
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow)
            values (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 0)
            """,
            [
                ("1A850001", yesterday.isoformat(), 10, 1000),
                ("1A850001", today.isoformat(), 20, 2000),
            ],
        )

    response = logged_in_client.get("/dashboard/trends/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Today vs. yesterday" in body
    assert response.context["txn_count_comparison"].current_value == decimal.Decimal("20")
    assert response.context["txn_count_comparison"].previous_value == decimal.Decimal("10")
    assert response.context["txn_count_comparison"].trend == "GROWTH"


@pytest.mark.django_db
def test_trends_page_transaction_comparison_no_data_without_mart(logged_in_client, db):
    response = logged_in_client.get("/dashboard/trends/")
    assert response.status_code == 200
    assert response.context["txn_count_comparison"].trend == "NO_DATA"


@pytest.mark.django_db
def test_trends_transaction_comparison_anchors_on_mart_not_wall_clock_today(logged_in_client, db):
    """The transaction workbook is downloaded/updated once a day and is
    already a full day behind by the time it's ingested -- wall-clock
    "today" almost never has a real transaction row. The comparison must
    anchor on the mart's own latest real date (same as views.transactions),
    not dt.date.today(), or this widget reads NO_DATA every single day."""
    from django.db import connection

    _create_daily_activity_mart()
    real_today = dt.date.today()
    # Simulate the real 1-day ingestion lag: the mart's most recent rows are
    # for (real_today - 1) and (real_today - 2), never for real_today itself.
    mart_latest = real_today - dt.timedelta(days=1)
    mart_prior = real_today - dt.timedelta(days=2)
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow)
            values (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 0)
            """,
            [
                ("1A850001", mart_prior.isoformat(), 10, 1000),
                ("1A850001", mart_latest.isoformat(), 20, 2000),
            ],
        )

    response = logged_in_client.get("/dashboard/trends/")
    comparison_result = response.context["txn_count_comparison"]
    assert comparison_result.current_value == decimal.Decimal("20")
    assert comparison_result.previous_value == decimal.Decimal("10")
    assert comparison_result.current_date == mart_latest
    assert comparison_result.comparison_date == mart_prior
    assert comparison_result.current_date != real_today


@pytest.mark.django_db
def test_trends_page_shows_network_account_growth(logged_in_client, csp_with_summary):
    from csp.models import DailyBalance

    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 1),
        daily_avg_balance=decimal.Decimal("3000"), account_count=500,
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp_with_summary, balance_date=dt.date(2026, 8, 2),
        daily_avg_balance=decimal.Decimal("3000"), account_count=520,
        source=DailyBalance.Source.CALLING_SHEET,
    )

    response = logged_in_client.get("/dashboard/trends/")
    body = response.content.decode()
    assert "Account growth, network-wide" in body
    assert "+20" in body
    assert "'value': 520" in body


@pytest.mark.django_db
def test_trends_page_date_range_filter(logged_in_client, db):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            create table if not exists daily_activity (
                csp_code varchar(32), activity_date date, txn_count int,
                txn_amount numeric, withdrawal_count int, withdrawal_amount numeric,
                deposit_count int, deposit_amount numeric, cash_in_pool numeric,
                cash_out_pool numeric, net_flow numeric,
                onus_txn_count int, onus_txn_amount numeric,
                onus_withdrawal_count int, onus_withdrawal_amount numeric,
                onus_deposit_count int, onus_deposit_amount numeric,
                onus_cash_in_pool numeric, onus_cash_out_pool numeric, onus_net_flow numeric
            )
            """
        )
        cursor.execute("delete from daily_activity")
        cursor.executemany(
            """
            insert into daily_activity
                (csp_code, activity_date, txn_count, txn_amount, withdrawal_count,
                 withdrawal_amount, deposit_count, deposit_amount, cash_in_pool,
                 cash_out_pool, net_flow)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                ("1A850001", "2026-08-01", 10, 1000, 6, 600, 4, 400, 600, 400, 200),
                ("1A850001", "2026-08-10", 40, 4000, 20, 2000, 20, 2000, 2000, 2000, 0),
            ],
        )
        try:
            # Default (no from/to): full available range shown in the picker.
            response = logged_in_client.get("/dashboard/trends/")
            body = response.content.decode()
            assert response.status_code == 200
            assert 'name="from" value="2026-08-01"' in body
            assert 'name="to" value="2026-08-10"' in body

            # Explicit range: only the matching day's activity is charted.
            response = logged_in_client.get("/dashboard/trends/?from=2026-08-01&to=2026-08-01")
            body = response.content.decode()
            assert "'value': 600.0" in body  # aug-1 withdrawal_amount
            assert "'value': 2000.0" not in body  # aug-10 withdrawal/deposit_amount, excluded
        finally:
            cursor.execute("drop table if exists daily_activity")


@pytest.mark.django_db
def test_api_docs_page_requires_login(client):
    response = client.get("/dashboard/api-docs/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_api_docs_page_embeds_the_real_docs_url(logged_in_client, db):
    response = logged_in_client.get("/dashboard/api-docs/")
    assert response.status_code == 200
    assert 'src="/api/v1/docs"' in response.content.decode()


@pytest.mark.django_db
def test_api_docs_page_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/api-docs/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_embed_requires_login(client):
    response = client.get("/dashboard/admin-console/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_admin_embed_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/admin-console/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_embed_renders_for_staff(logged_in_client):
    response = logged_in_client.get("/dashboard/admin-console/")
    assert response.status_code == 200
    assert 'src="/admin/"' in response.content.decode()


@pytest.mark.django_db
@override_settings(URL_PREFIX="/csp-balance-intelligence")
def test_admin_embed_iframe_honors_url_prefix(logged_in_client):
    """Under a subpath deployment, a bare "/admin/" iframe src is routed by
    the outer Nginx to a DIFFERENT app's location block on the shared
    server, not ours -- it must carry URL_PREFIX (see context_processors.
    dashboard_shell) so the browser actually reaches our own app."""
    response = logged_in_client.get("/dashboard/admin-console/")
    assert response.status_code == 200
    assert 'src="/csp-balance-intelligence/admin/"' in response.content.decode()


@pytest.mark.django_db
@override_settings(URL_PREFIX="/csp-balance-intelligence")
def test_api_docs_iframe_honors_url_prefix(logged_in_client, db):
    response = logged_in_client.get("/dashboard/api-docs/")
    assert response.status_code == 200
    assert 'src="/csp-balance-intelligence/api/v1/docs"' in response.content.decode()


@pytest.mark.django_db
def test_messaging_requires_login(client):
    response = client.get("/dashboard/messaging/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_messaging_forbidden_for_non_staff(viewer_client):
    response = viewer_client.get("/dashboard/messaging/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_messaging_lists_drafts_for_staff(logged_in_client, csp_with_summary):
    from autopilot.models import DraftMessage

    DraftMessage.objects.create(csp=csp_with_summary, text="You're close to S2!")
    response = logged_in_client.get("/dashboard/messaging/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "You&#x27;re close to S2!" in body or "You're close to S2!" in body
    assert "Draft" in body


@pytest.mark.django_db
def test_messaging_empty_state(logged_in_client, db):
    response = logged_in_client.get("/dashboard/messaging/")
    assert "No draft messages yet" in response.content.decode()


@pytest.mark.django_db
def test_messaging_status_filter(logged_in_client, csp_with_summary):
    from autopilot.models import DraftMessage

    DraftMessage.objects.create(
        csp=csp_with_summary, text="Draft one", status=DraftMessage.Status.DRAFT
    )
    DraftMessage.objects.create(
        csp=csp_with_summary, text="Approved one", status=DraftMessage.Status.APPROVED
    )

    response = logged_in_client.get("/dashboard/messaging/?status=approved")
    body = response.content.decode()
    assert "Approved one" in body
    assert "Draft one" not in body


@pytest.mark.django_db
def test_messaging_approve_action_updates_status_and_reviewer(
    logged_in_client, staff_user, csp_with_summary
):
    from autopilot.models import DraftMessage

    draft = DraftMessage.objects.create(
        csp=csp_with_summary, text="Hello", status=DraftMessage.Status.DRAFT
    )
    response = logged_in_client.post(
        "/dashboard/messaging/", {"message_id": draft.id, "action": "approve"}
    )
    assert response.status_code == 302

    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.APPROVED
    assert draft.reviewed_by_id == staff_user.id
    assert draft.reviewed_at is not None


@pytest.mark.django_db
def test_messaging_reject_action_updates_status(logged_in_client, csp_with_summary):
    from autopilot.models import DraftMessage

    draft = DraftMessage.objects.create(
        csp=csp_with_summary, text="Hello", status=DraftMessage.Status.DRAFT
    )
    logged_in_client.post("/dashboard/messaging/", {"message_id": draft.id, "action": "reject"})

    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.REJECTED


@pytest.mark.django_db
def test_messaging_approve_forbidden_for_non_staff(viewer_client, csp_with_summary):
    from autopilot.models import DraftMessage

    draft = DraftMessage.objects.create(
        csp=csp_with_summary, text="Hello", status=DraftMessage.Status.DRAFT
    )
    response = viewer_client.post(
        "/dashboard/messaging/", {"message_id": draft.id, "action": "approve"}
    )
    assert response.status_code == 403

    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.DRAFT  # untouched


@pytest.mark.django_db
def test_nav_hides_staff_only_links_for_viewer(viewer_client, csp_with_summary):
    response = viewer_client.get("/dashboard/")
    body = response.content.decode()
    assert "Overview" in body
    assert "Trends" in body
    assert 'dashboard:api_docs' not in body
    assert ">API Documentation<" not in body
    assert ">Admin<" not in body
    assert ">Messaging<" not in body


@pytest.mark.django_db
def test_nav_shows_staff_only_links_for_staff(logged_in_client, csp_with_summary):
    response = logged_in_client.get("/dashboard/")
    body = response.content.decode()
    assert ">API Documentation<" in body
    assert ">Administration<" in body
    assert ">Messaging<" in body


@pytest.mark.django_db
def test_viewer_can_still_see_overview_and_trends(viewer_client, csp_with_summary):
    assert viewer_client.get("/dashboard/").status_code == 200
    assert viewer_client.get("/dashboard/trends/").status_code == 200
    assert viewer_client.get(f"/dashboard/csp/{csp_with_summary.csp_code}/").status_code == 200


@pytest.mark.django_db
def test_iframe_friendly_frame_options(logged_in_client, db):
    # SAMEORIGIN, not the Django default DENY — required for the api-docs
    # iframe above to render at all; still blocks every other origin.
    response = logged_in_client.get("/dashboard/")
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
