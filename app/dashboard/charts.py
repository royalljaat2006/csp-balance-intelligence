"""Chart specifications for static/dashboard/app.js (rendered from a json_script element).

A spec is plain data: the browser draws it. Points are [iso date or label, number | None]."""

from __future__ import annotations


def spec(label: str, unit: str, series: list[dict], *, kind: str = "line", empty: str = "",
         categorical: bool = False) -> dict:
    return {"type": kind, "unit": unit, "label": label, "series": series, "empty": empty,
            "categorical": categorical}


def querystring(request, **over) -> str:
    """The current query string with some values replaced (used for pager links)."""
    from urllib.parse import urlencode

    q = {k: v for k, v in request.GET.items() if k != "page"}
    q.update({k: v for k, v in over.items() if v not in (None, "")})
    return urlencode(q)


def points(series: list[dict]) -> list[list]:
    """[{"date": d, "value": v}] -> [[iso date, v]] for a chart series."""
    return [[str(r["date"]), r["value"]] for r in series]
