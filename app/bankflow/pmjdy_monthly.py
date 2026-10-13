"""
New PMJDY accounts per CSP, read from the Calling Sheet's per-month column blocks.

Every month the main tab ("Calling Sheet New") gains blocks whose row-1 label names the month, e.g.
"Targets Oct 2026" (header "PMJDY Monthly"), "MTD Achievement October'26" (header "PMJDY") and
"FTD Achievement October'26" (header "PMJDY"). Row 2 holds the column headers, and the CSP
code is in the "CSP ID" column. This module finds THIS MONTH's blocks (IST), reads the PMJDY
column of each, and keeps an insert-only history (PmjdyMonthly).

Rules, so nothing is guessed:
  * a month's blocks are matched on the month in the label (and the year, when the label has one);
  * the MTD block is required: if this month's block is missing (e.g. the 1st of a new month before
    the sheet owner adds it) the read is REFUSED -- last month's numbers are never reused;
  * two blocks of the same kind for the same month are ambiguous and refused;
  * a blank or non-numeric cell is "no reading", never zero;
  * a CSP listed twice with different numbers is skipped and reported.
These are accounts OPENED (month-to-date, for-the-day, target) -- not the total account base.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

from csp.models import Csp
from django.db import transaction
from django.utils import timezone
from ingestion.calling_sheet_ingest import parse_int

from bankflow.models import PmjdyMonthly

HEADER_ROW = 2  # row 1 = merged section labels, row 2 = column headers (as in calling_sheet_ingest)
KINDS = {"mtd": re.compile(r"\bmtd\s+achievement\b", re.I),
         "ftd": re.compile(r"\bftd\s+achievement\b", re.I),
         "target": re.compile(r"^\s*targets?\b", re.I)}


class PmjdyValidationError(ValueError):
    pass


_MONTHS = {m: i for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
     ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
     ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], start=1) for m in names}


def label_month(label: str) -> tuple[int | None, int] | None:
    """(year or None, month) from a section label like "MTD Achievement October'26" or
    "Targets Oct 2026". The first month name wins; a year is only used if the label states one."""
    t = label.lower().replace("_", " ").replace("/", " ")
    for m in re.finditer(r"\b([a-z]{3,9})\b\W{0,2}(?:(?:20)?(\d{2}))?", t):
        month = _MONTHS.get(m.group(1))
        if month:
            return (2000 + int(m.group(2)) if m.group(2) else None), month
    return None


@dataclass
class MonthlyParse:
    month: str  # "YYYY-MM"
    rows: dict[str, dict] = field(default_factory=dict)  # code -> {"mtd","ftd","target"}
    labels: dict[str, str] = field(default_factory=dict)  # kind -> the sheet label used
    rows_read: int = 0
    skipped_blank_code: int = 0
    conflicting_codes: list[str] = field(default_factory=list)


def _spans(row1: list[str]) -> list[tuple[str, int, int]]:
    """Merged section labels: a label covers its own column up to the next non-empty label."""
    starts = [(j, v.strip()) for j, v in enumerate(row1) if v and v.strip()]
    return [(lab, j, (starts[i + 1][0] if i + 1 < len(starts) else len(row1)))
            for i, (j, lab) in enumerate(starts)]


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", v or "").strip()


def parse_monthly_pmjdy(
    all_values: list[list[str]], today: dt.date, *, code_header: str = "CSP ID",
    column: str = "PMJDY",
) -> MonthlyParse:
    if len(all_values) <= HEADER_ROW:
        raise PmjdyValidationError(f"Sheet has fewer than {HEADER_ROW + 1} rows")
    row1, row2 = all_values[0], [_norm(h) for h in all_values[HEADER_ROW - 1]]
    spans = _spans(row1)
    try:
        code_ix = [h.lower() for h in row2].index(code_header.lower())
    except ValueError as exc:
        raise PmjdyValidationError(f"Column {code_header!r} not found in row {HEADER_ROW}") from exc

    chosen: dict[str, int] = {}  # kind -> column index of the PMJDY cell
    labels: dict[str, str] = {}
    for kind, pat in KINDS.items():
        hits = []
        for label, a, b in spans:
            ym = label_month(label)
            if pat.search(label) and ym and ym[1] == today.month and ym[0] in (None, today.year):
                hits.append((label, a, b))
        if len(hits) > 1:
            names = [h[0] for h in hits]
            raise PmjdyValidationError(
                f"Ambiguous: several {kind.upper()} blocks match {today:%Y-%m}: {names}")
        if hits:
            label, a, b = hits[0]
            cols = [j for j in range(a, min(b, len(row2))) if column.lower() in row2[j].lower()]
            if len(cols) != 1:
                if kind == "mtd":
                    raise PmjdyValidationError(
                        f"Block {label!r} has {len(cols)} columns containing {column!r}; "
                        "need exactly 1")
                continue
            chosen[kind], labels[kind] = cols[0], label
    if "mtd" not in chosen:
        seen = [lab for lab, _a, _b in spans if KINDS["mtd"].search(lab)]
        raise PmjdyValidationError(
            f"No 'MTD Achievement' block for {today:%Y-%m} (blocks seen: {seen}); "
            "last month's numbers are never reused")

    out = MonthlyParse(month=f"{today:%Y-%m}", labels=labels)
    seen_vals: dict[str, set[tuple]] = defaultdict(set)
    for raw in all_values[HEADER_ROW:]:
        code = raw[code_ix].strip() if len(raw) > code_ix else ""
        if not code:
            out.skipped_blank_code += 1
            continue
        out.rows_read += 1
        vals = {}
        for kind, j in chosen.items():
            v = parse_int(raw[j]) if len(raw) > j else None
            vals[kind] = v if v is not None and v >= 0 else None
        seen_vals[code].add((vals.get("mtd"), vals.get("ftd"), vals.get("target")))
    for code, variants in seen_vals.items():
        if len(variants) > 1:
            out.conflicting_codes.append(code)
        else:
            mtd, ftd, target = next(iter(variants))
            out.rows[code] = {"mtd": mtd, "ftd": ftd, "target": target}
    out.conflicting_codes.sort()
    return out


def latest_monthly(month: str, csp_ids=None) -> dict[str, PmjdyMonthly]:
    qs = PmjdyMonthly.objects.filter(month=month)
    if csp_ids is not None:
        qs = qs.filter(csp_id__in=list(csp_ids))
    return {r.csp_id: r for r in qs.order_by("csp_id", "-observed_at").distinct("csp_id")}


def capture_monthly(parsed: MonthlyParse, observed_at: dt.datetime | None = None) -> dict:
    """Insert-only and idempotent: a CSP gets a new row only if a figure changed or it has no row
    for this IST day. Unknown CSP codes are reported, never created."""
    observed_at = observed_at or timezone.now()
    snap_date = timezone.localtime(observed_at).date()
    known = set(Csp.objects.filter(csp_code__in=parsed.rows).values_list("csp_code", flat=True))
    latest = latest_monthly(parsed.month, known)
    have_today = set(
        PmjdyMonthly.objects.filter(month=parsed.month, snapshot_date=snap_date, csp_id__in=known)
        .values_list("csp_id", flat=True))
    new, changed, decreased = [], 0, 0
    for code in known:
        v = parsed.rows[code]
        prev = latest.get(code)
        same = prev is not None and (prev.mtd, prev.ftd, prev.target) == (
            v["mtd"], v["ftd"], v["target"])
        if not same or code not in have_today:
            new.append(PmjdyMonthly(csp_id=code, month=parsed.month, mtd=v["mtd"], ftd=v["ftd"],
                                    target=v["target"], observed_at=observed_at,
                                    snapshot_date=snap_date))
            if prev is not None and not same:
                changed += 1
                decreased += (v["mtd"] is not None and prev.mtd is not None and v["mtd"] < prev.mtd)
    with transaction.atomic():
        PmjdyMonthly.objects.bulk_create(new, ignore_conflicts=True, batch_size=1000)
    return {"inserted": len(new), "changed": changed, "mtd_decreased": decreased,
            "unchanged_not_inserted": len(known) - len(new),
            "n_unknown_codes": len(set(parsed.rows) - known),
            "unknown_codes": sorted(set(parsed.rows) - known)[:50], "snapshot_date": snap_date}


def monthly_totals(month: str) -> dict | None:
    """Network totals of the newest reading per CSP for `month`, or None if nothing captured."""
    rows = list(latest_monthly(month).values())
    if not rows:
        return None
    def total(attr):
        vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        return (sum(vals), len(vals)) if vals else (None, 0)
    mtd, n_mtd = total("mtd")
    ftd, _ = total("ftd")
    target, _ = total("target")
    return {"month": month, "mtd": mtd, "ftd": ftd, "target": target, "csps_reporting": n_mtd,
            "observed_at": max(r.observed_at for r in rows)}
