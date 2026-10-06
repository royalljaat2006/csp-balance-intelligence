"""
csp.services' network-wide transaction readers — list_transactions(csp=None)
(the Transactions Intelligence page's own listing), get_active_transaction_
csp_count(), get_transaction_type_distribution(). The per-CSP behavior of
list_transactions(csp=...) is unchanged and already covered by
test_dashboard.py's csp_detail tests; these are about the new network-wide
path specifically.
"""

import datetime as dt
import decimal

import pytest
from csp import services
from csp.models import Csp, Transaction


@pytest.fixture
def two_csps(db):
    a = Csp.objects.create(csp_code="1A850001", name="CSP A")
    b = Csp.objects.create(csp_code="1A850002", name="CSP B")
    return a, b


def _txn(csp, ref, txn_type, amount, txn_date, category="withdrawal", direction="in_pool"):
    return Transaction.objects.create(
        ref_number=ref, csp=csp, txn_datetime=dt.datetime.combine(txn_date, dt.time(10, 0)),
        txn_date=txn_date, txn_type=txn_type, category=category, direction=direction,
        amount=decimal.Decimal(str(amount)),
    )


@pytest.mark.django_db
def test_list_transactions_network_wide_without_csp(two_csps):
    a, b = two_csps
    today = dt.date.today()
    _txn(a, "R1", "AEPS ONUS Withdrawal", 1000, today)
    _txn(b, "R2", "Deposit", 2000, today)

    rows, total = services.list_transactions()
    assert total == 2
    assert {r.ref_number for r in rows} == {"R1", "R2"}


@pytest.mark.django_db
def test_list_transactions_onus_only_filters_to_onus_types(two_csps):
    a, b = two_csps
    today = dt.date.today()
    _txn(a, "R1", "AEPS ONUS Withdrawal", 1000, today)
    _txn(b, "R2", "Deposit", 2000, today)

    rows, total = services.list_transactions(onus_only=True)
    assert total == 1
    assert rows[0].ref_number == "R1"


@pytest.mark.django_db
def test_list_transactions_still_scopes_to_one_csp_when_given(two_csps):
    a, b = two_csps
    today = dt.date.today()
    _txn(a, "R1", "AEPS ONUS Withdrawal", 1000, today)
    _txn(b, "R2", "Deposit", 2000, today)

    rows, total = services.list_transactions(a)
    assert total == 1
    assert rows[0].ref_number == "R1"


@pytest.mark.django_db
def test_get_active_transaction_csp_count(two_csps):
    a, b = two_csps
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    _txn(a, "R1", "Deposit", 1000, today)
    _txn(b, "R2", "Deposit", 2000, today)
    _txn(a, "R3", "Deposit", 500, yesterday)

    assert services.get_active_transaction_csp_count(today) == 2
    assert services.get_active_transaction_csp_count(yesterday) == 1


@pytest.mark.django_db
def test_get_transaction_type_distribution(two_csps):
    a, b = two_csps
    today = dt.date.today()
    _txn(a, "R1", "AEPS ONUS Withdrawal", 1000, today)
    _txn(b, "R2", "AEPS ONUS Withdrawal", 500, today)
    _txn(a, "R3", "Deposit", 200, today)

    dist = {d["txn_type"]: d for d in services.get_transaction_type_distribution()}
    assert dist["AEPS ONUS Withdrawal"]["count"] == 2
    assert dist["AEPS ONUS Withdrawal"]["amount"] == decimal.Decimal("1500")
    assert dist["Deposit"]["count"] == 1
