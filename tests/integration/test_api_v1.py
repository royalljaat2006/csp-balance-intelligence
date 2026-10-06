"""
End-to-end checks for every /api/v1 resource: seeded data in -> correct
envelope + fields out. Complements test_api_auth.py (which covers auth
itself) by focusing on response shape/content per resource.
"""

import datetime as dt
import decimal

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from csp.models import Csp, DailyBalance, MonthlySummary, Transaction


@pytest.fixture
def api_key(db):
    consumer = ApiConsumer.objects.create(name="test-suite")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer,
        key_prefix=raw[:12],
        key_hash=hash_key(raw),
        scopes="csp:read,balance:read,incentive:read,projection:read,transaction:read,report:read",
    )
    return raw


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850583", name="Test CSP", mobile="9999999999")


@pytest.fixture
def month():
    return dt.date.today().strftime("%Y-%m")


@pytest.fixture
def summary(csp, month):
    return MonthlySummary.objects.create(
        csp=csp,
        month=month,
        days_in_month=30,
        days_with_data=10,
        mtd_mab=decimal.Decimal("2600.00"),
        projected_mab=decimal.Decimal("2550.00"),
        slab=MonthlySummary.Slab.S1,
        projected_slab=MonthlySummary.Slab.S1,
        incentive_rate_pa=decimal.Decimal("1.10"),
        projected_incentive_annual=decimal.Decimal("10000.00"),
        gap_to_min=decimal.Decimal("0.00"),
        gap_to_next_slab=decimal.Decimal("1400.00"),
        trend_flag=MonthlySummary.Trend.IMPROVING,
        is_eligible=True,
    )


def auth_headers(key):
    return {"X-API-Key": key}


@pytest.mark.django_db
def test_list_csps(client, api_key, summary):
    resp = client.get("/api/v1/csps", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert body["data"][0]["csp_code"] == "1A850583"
    assert body["data"][0]["slab"] == "S1"
    assert "request_id" in body


@pytest.mark.django_db
def test_get_csp_detail(client, api_key, csp, summary):
    resp = client.get(f"/api/v1/csps/{csp.csp_code}", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["csp_code"] == csp.csp_code
    assert data["summary"]["slab"] == "S1"


@pytest.mark.django_db
def test_get_csp_detail_unknown_returns_rfc9457_404(client, api_key):
    resp = client.get("/api/v1/csps/DOES-NOT-EXIST", headers=auth_headers(api_key))
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == 404
    assert "request_id" in body
    assert "title" in body and "detail" in body


@pytest.mark.django_db
def test_current_balance(client, api_key, csp):
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 1), daily_avg_balance=2500, source="calling_sheet"
    )
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 2), daily_avg_balance=2700, source="calling_sheet"
    )
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/balance", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["date"] == "2026-09-02"
    assert data["daily_avg_balance"] == 2700.0


@pytest.mark.django_db
def test_balance_history(client, api_key, csp):
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 1), daily_avg_balance=2500, source="calling_sheet"
    )
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/balances/history", headers=auth_headers(api_key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert body["data"][0]["daily_avg_balance"] == 2500.0


@pytest.mark.django_db
def test_incentive(client, api_key, csp, summary):
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/incentive", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["slab"] == "S1"
    assert data["incentive_rate_pa"] == 1.10
    assert data["gap_to_next_slab"] == 1400.0


@pytest.mark.django_db
def test_projection(client, api_key, csp, summary):
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/projection", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["projected_mab"] == 2550.0
    assert data["trend_flag"] == "improving"


@pytest.mark.django_db
def test_transactions_empty_until_ingestion_writes_rows(client, api_key, csp):
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/transactions", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0


@pytest.mark.django_db
def test_transactions_lists_seeded_rows(client, api_key, csp):
    Transaction.objects.create(
        ref_number="REF1",
        csp=csp,
        txn_datetime=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC),
        txn_date=dt.date(2026, 9, 1),
        txn_type="AEPS ONUS Withdrawal",
        category=Transaction.Category.WITHDRAWAL,
        direction=Transaction.Direction.IN_POOL,
        amount=2000,
    )
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/transactions", headers=auth_headers(api_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert body["data"][0]["ref_number"] == "REF1"
    assert body["data"][0]["category"] == "withdrawal"


@pytest.mark.django_db
def test_reports_overview(client, api_key, summary):
    resp = client.get("/api/v1/reports/overview", headers=auth_headers(api_key))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["csp_total"] == 1
    assert data["csp_with_data"] == 1
    assert data["slab_distribution"]["S1"] == 1
    assert data["in_nil_count"] == 0


@pytest.mark.django_db
def test_response_carries_request_id_header(client, api_key):
    resp = client.get("/api/v1/reports/overview", headers=auth_headers(api_key))
    assert "X-Request-ID" in resp.headers
    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]


@pytest.mark.django_db
def test_client_supplied_request_id_is_honoured_if_safe(client, api_key):
    resp = client.get(
        "/api/v1/reports/overview",
        headers={**auth_headers(api_key), "X-Request-ID": "my-trace-id-123"},
    )
    assert resp.headers["X-Request-ID"] == "my-trace-id-123"


@pytest.mark.django_db
def test_unsafe_request_id_is_replaced(client, api_key):
    resp = client.get(
        "/api/v1/reports/overview",
        headers={**auth_headers(api_key), "X-Request-ID": "not safe! <script>"},
    )
    assert resp.headers["X-Request-ID"] != "not safe! <script>"


@pytest.mark.django_db
def test_scope_mismatch_returns_rfc9457_403(client, csp, summary):
    consumer = ApiConsumer.objects.create(name="csp-only")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes="csp:read"
    )
    resp = client.get(f"/api/v1/csps/{csp.csp_code}/incentive", headers=auth_headers(raw))
    assert resp.status_code == 403
    body = resp.json()
    assert body["status"] == 403
    assert "request_id" in body
