"""Money is always shown as the FULL rupee amount (Indian digit grouping), never as lakh/crore."""

from decimal import Decimal as D

import pytest
from dashboard.templatetags import fmt

MINUS = "−"


@pytest.mark.parametrize("value, expected", [
    (D("3000"), "₹3,000"),
    (D("456440"), "₹4,56,440"),
    (D("45644640"), "₹4,56,44,640"),  # 4.56 crore is written in full
    (D("959207882.00"), "₹95,92,07,882"),
    (D("734222.95"), "₹7,34,222.95"),  # paise appear only when there are some
    (D("0"), "₹0"),
    (-25219, MINUS + "₹25,219"),
    (D("-1234567.5"), MINUS + "₹12,34,567.50"),
    (None, "—"),
    ("", "—"),
])
def test_inr_is_the_full_amount(value, expected):
    assert fmt.inr(value) == expected


def test_signed_inr_marks_gains_and_losses_in_full():
    assert fmt.signed_inr(D("11540000")) == "+₹1,15,40,000"
    assert fmt.signed_inr(D("-25219")) == MINUS + "₹25,219"
    assert fmt.signed_inr(D("0")) == "₹0"
    assert fmt.signed_inr(None) == "—"


def test_no_short_forms_survive_anywhere():
    for amount in (D("99999"), D("100000"), D("9999999"), D("10000000"), D("123456789")):
        for out in (fmt.inr(amount), fmt.signed_inr(amount)):
            assert not any(tag in out for tag in ("Cr", " L", "L ", "crore", "lakh", "K"))
            assert out.replace(",", "").lstrip("+").lstrip("₹").isdigit()


def test_counts_use_the_same_grouping_but_no_rupee_sign():
    assert fmt.intc(802107) == "8,02,107"
    assert fmt.signed(1208) == "+1,208"
