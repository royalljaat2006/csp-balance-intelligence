"""
/api/v1/csps/{code}/comparison, /trend, /mtd-readiness, and the network-wide
/api/v1/comparisons, /comparisons/top-movers, /comparisons/at-risk — the API
surface over csp/comparison.py. Response-shape/content checks; the
comparison math itself is covered by test_comparison_engine.py.
"""

import datetime as dt
import decimal

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from csp.models import Csp, DailyBalance


@pytest.fixture
def api_key(db):
    consumer = ApiConsumer.objects.create(name="test-suite")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes="comparison:read"
    )
    return raw


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850700", name="Comparison Test CSP", account_count=300)


def auth_headers(key):
    return {"X-API-Key": key}


def _balance(csp, date, amount):
    DailyBalance.objects.create(
        csp=csp, balance_date=date, daily_avg_balance=decimal.Decimal(str(amount)),
        source=DailyBalance.Source.CALLING_SHEET,
    )


@pytest.mark.django_db
def test_requires_comparison_scope(client, csp):
    consumer = ApiConsumer.objects.create(name="no-scope")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes="csp:read"
    )
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/comparison", headers=auth_headers(raw)
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_csp_comparison_defaults_to_yesterday(client, api_key, csp):
    _balance(csp, dt.date(2026, 9, 26), 5000)
    _balance(csp, dt.date(2026, 9, 27), 5500)
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/comparison?current_date=2026-09-27",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["comparison_date"] == "2026-09-26"
    assert data["balance"]["current_value"] == 5500.0
    assert data["balance"]["previous_value"] == 5000.0
    assert data["balance"]["trend"] == "GROWTH"
    assert "request_id" in resp.json()


@pytest.mark.django_db
def test_csp_comparison_same_date_previous_month(client, api_key, csp):
    _balance(csp, dt.date(2026, 8, 27), 4000)
    _balance(csp, dt.date(2026, 9, 27), 5000)
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/comparison"
        "?current_date=2026-09-27&comparison_type=same_date_previous_month",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["comparison_date"] == "2026-08-27"
    assert data["balance"]["previous_value"] == 4000.0


@pytest.mark.django_db
def test_csp_comparison_explicit_date_overrides_type(client, api_key, csp):
    _balance(csp, dt.date(2026, 7, 1), 1000)
    _balance(csp, dt.date(2026, 9, 27), 2000)
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/comparison"
        "?current_date=2026-09-27&comparison_type=yesterday&comparison_date=2026-07-01",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["comparison_date"] == "2026-07-01"


@pytest.mark.django_db
def test_csp_comparison_unknown_csp_is_404(client, api_key):
    resp = client.get(
        "/api/v1/csps/DOES-NOT-EXIST/comparison", headers=auth_headers(api_key)
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_csp_trend_reports_reversal(client, api_key, csp):
    values = [5000, 4800, 4600, 4400, 4600, 4900]
    for i, val in enumerate(values):
        _balance(csp, dt.date(2026, 9, 1) + dt.timedelta(days=i), val)
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/trend?as_of_date=2026-09-06",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["trend_reversal"] == "DECLINE_TO_GROWTH"
    assert data["consecutive_growth_days"] == 2


@pytest.mark.django_db
def test_csp_mtd_readiness(client, api_key, csp):
    _balance(csp, dt.date(2026, 9, 1), 3000)
    _balance(csp, dt.date(2026, 9, 2), 3200)
    resp = client.get(
        f"/api/v1/csps/{csp.csp_code}/mtd-readiness?as_of_date=2026-09-02",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_mtd"] == 3100.0
    assert data["current_days"] == 2


@pytest.mark.django_db
def test_list_comparisons_filters_by_trend(client, api_key, db):
    growing = Csp.objects.create(csp_code="1A850701", name="Growing")
    declining = Csp.objects.create(csp_code="1A850702", name="Declining")
    _balance(growing, dt.date(2026, 9, 26), 3000)
    _balance(growing, dt.date(2026, 9, 27), 4000)
    _balance(declining, dt.date(2026, 9, 26), 4000)
    _balance(declining, dt.date(2026, 9, 27), 3000)

    resp = client.get(
        "/api/v1/comparisons?current_date=2026-09-27&trend=GROWTH",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    codes = [row["csp_code"] for row in body["data"]]
    assert "1A850701" in codes
    assert "1A850702" not in codes
    assert "pagination" in body


@pytest.mark.django_db
def test_list_comparisons_search_uses_db_pushdown_and_matches_existing_semantics(
    client, api_key, db
):
    """resolve_matching_csp_codes() (scalability fast-pass, 2026-09-21)
    pre-filters at the DB layer before bulk_compare_csps() runs — the
    externally-visible result must be identical to the old
    filter-everything-in-Python behaviour."""
    Csp.objects.create(csp_code="1A850801", name="Alpha Traders")
    Csp.objects.create(csp_code="1A850802", name="Beta Traders")

    resp = client.get(
        "/api/v1/comparisons?current_date=2026-09-27&search=Alpha",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    codes = [row["csp_code"] for row in resp.json()["data"]]
    assert codes == ["1A850801"]


@pytest.mark.django_db
def test_list_comparisons_slab_filter_with_no_matches_returns_empty(client, api_key, db):
    Csp.objects.create(csp_code="1A850803", name="No Slab Yet")
    resp = client.get(
        "/api/v1/comparisons?current_date=2026-09-27&slab=S4",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


@pytest.mark.django_db
def test_list_comparisons_pagination(client, api_key, db):
    for i in range(3):
        Csp.objects.create(csp_code=f"1A85080{i}", name=f"CSP {i}")
    resp = client.get(
        "/api/v1/comparisons?current_date=2026-09-27&limit=1&offset=1",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["limit"] == 1
    assert body["pagination"]["offset"] == 1
    assert body["pagination"]["total"] == 3
    assert len(body["data"]) == 1


@pytest.mark.django_db
def test_top_movers(client, api_key, db):
    grower = Csp.objects.create(csp_code="1A850710", name="Grower")
    _balance(grower, dt.date(2026, 9, 26), 1000)
    _balance(grower, dt.date(2026, 9, 27), 2000)

    resp = client.get(
        "/api/v1/comparisons/top-movers?current_date=2026-09-27&direction=growth",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["csp_code"] == "1A850710"


@pytest.mark.django_db
def test_at_risk_returns_explainable_reasons(client, api_key, db):
    from csp import comparison as cmp

    risky = Csp.objects.create(csp_code="1A850720", name="Risky", account_count=250)
    _balance(risky, dt.date(2026, 9, 26), 2540)
    _balance(risky, dt.date(2026, 9, 27), 2510)
    cmp.build_daily_snapshot(risky.csp_code, dt.date(2026, 9, 27))

    resp = client.get(
        "/api/v1/comparisons/at-risk?current_date=2026-09-27&comparison_date=2026-09-26"
        "&near_threshold=50",
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["csp_code"] == "1A850720"
    assert any("eligibility minimum" in r for r in body["data"][0]["reasons"])
