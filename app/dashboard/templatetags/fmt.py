"""Display-only formatting for the decision dashboard. No business logic."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()
MINUS = "−"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _indian(n: int) -> str:
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + out


def _full(n: Decimal) -> str:
    """Full rupee amount, Indian digit grouping; paise only when there are any."""
    q = abs(n).quantize(Decimal("0.01"))
    whole, frac = f"{q:.2f}".split(".")
    grouped = _indian(int(whole))
    return f"\u20b9{grouped}" + ("" if frac == "00" else f".{frac}")


@register.filter
def inr(value):
    """The full amount, e.g. 4,56,44,640 rupees; a minus sign for negatives; empty for missing."""
    n = _num(value)
    if n is None:
        return "\u2014"
    return (MINUS if n < 0 else "") + _full(n)


@register.filter
def signed(value):
    """+90 / −3 / 0 for integer-ish changes."""
    n = _num(value)
    if n is None:
        return "—"
    i = int(n.to_integral_value())
    return f"+{_indian(i)}" if i > 0 else (MINUS + _indian(-i) if i < 0 else "0")


@register.filter
def signed_inr(value):
    """+/- full amount for differences."""
    n = _num(value)
    if n is None:
        return "\u2014"
    return ("+" if n > 0 else "") + inr(n)


@register.filter
def pct1(value):
    n = _num(value)
    if n is None:
        return "—"
    return f"{n:+.1f}%"


@register.filter
def tone(value):
    """CSS class for a signed number: up / down / flat."""
    n = _num(value)
    if n is None or n == 0:
        return "flat"
    return "up" if n > 0 else "down"


@register.filter
def pct0(value):
    n = _num(value)
    return "—" if n is None else f"{n * 100:.0f}%"


@register.filter
def intc(value):
    n = _num(value)
    return "—" if n is None else _indian(int(n))


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key, "missing")
    except AttributeError:
        return "missing"
