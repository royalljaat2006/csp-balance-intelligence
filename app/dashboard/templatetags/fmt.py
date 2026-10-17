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


# ---- tone and wording helpers: presentation only, they decide nothing --------------------------
_STATE_TONE = {"fresh": "ok", "ok": "ok", "on_time": "ok", "done": "ok", "complete": "ok",
               "delayed": "warn", "warn": "warn", "late": "warn", "waiting": "info",
               "partial": "warn", "info": "info", "success": "ok", "failed": "bad",
               "received": "info", "processing": "info", "duplicate_skipped": "info",
               "invalid_source": "bad", "imported": "ok", "quarantined": "warn",
               "stale": "bad", "missing": "bad", "never": "warn"}
_STATUS_WORD = {"ok": "Ready", "warn": "Check", "bad": "Problem"}
_EST_LABEL = {
    "VALIDATED": "Reliable", "PROVISIONAL": "Provisional",
    "INSUFFICIENT_EVIDENCE": "Shadow estimate, not validated", "UNAVAILABLE": "No estimate",
    "NOT_COMPUTED": "Not calculated yet",
}
_EST_TONE = {"VALIDATED": "ok", "PROVISIONAL": "warn", "INSUFFICIENT_EVIDENCE": "shadow",
             "UNAVAILABLE": "", "NOT_COMPUTED": ""}


@register.filter
def state_tone(value):
    """freshness / health word -> badge tone (anything unknown or bad reads as 'bad')."""
    return _STATE_TONE.get(str(value or "").lower(), "bad")


@register.filter
def status_word(value):
    return _STATUS_WORD.get(value, str(value or ""))


@register.filter
def sev_tone(value):
    return {"high": "bad", "medium": "warn"}.get(value, "info")


@register.filter
def est_label(value):
    return _EST_LABEL.get(value, str(value or "").replace("_", " ").capitalize())


@register.filter
def est_tone(value):
    return _EST_TONE.get(value, "")


@register.filter
def ago_days(value):
    """'today' / '1 day ago' / '5 days ago' for an integer number of days."""
    n = _num(value)
    if n is None:
        return "never"
    n = int(n)
    return "today" if n <= 0 else ("1 day ago" if n == 1 else f"{n} days ago")


@register.simple_tag(takes_context=True)
def page_url(context, page):
    """'?' + the current query string with ?page= replaced: pager links keep every filter."""
    from urllib.parse import urlencode

    request = context.get("request")
    q = {k: v for k, v in (request.GET.items() if request else []) if k != "page"}
    q["page"] = page
    return "?" + urlencode(q)
