"""Monthly PMJDY blocks in the main tab: this month's block, never a guess, never last month's."""

import datetime as dt

import pytest
from bankflow.pmjdy_monthly import PmjdyValidationError, label_month, parse_monthly_pmjdy

OCT = dt.date(2026, 10, 13)


def make_grid(blocks, rows):
    """blocks: [(row-1 label, [row-2 headers])]; rows: {code: [[block 1 cells], [block 2 cells]]}.
    Layout: CSP ID, Total Accounts, then the blocks in order."""
    row1, row2 = ["", ""], ["CSP ID", "Total Accounts"]
    for label, headers in blocks:
        row1 += [label] + [""] * (len(headers) - 1)  # merged cell: label only in its first column
        row2 += headers
    grid = [row1, row2]
    for code, per_block in rows.items():
        grid.append([code, "1,000"] + [str(c) for cells in per_block for c in cells])
    return grid


BLOCKS = [
    ("Targets Sep 2026", ["PMSBY Monthly", "PMJDY Monthly"]),
    ("Targets Oct 2026", ["PMSBY Monthly", "PMJDY\n Monthly"]),
    ("MTD Achievement September'26", ["PMJJBY", "PMJDY", "Slab"]),
    ("MTD Achievement October'26", ["PMJJBY", "PMSBY", "APY", "PMJDY", "Slab", "Status"]),
    ("FTD Achievement October'26", ["PMJJBY", "PMJDY", "Slab"]),
]


def rows(**cells):
    return {code: [[1, 111], [2, 222], [3, 333, "x"], [4, 5, 6, mtd, "Slab-1", "ok"], [7, ftd, "y"]]
            for code, (mtd, ftd) in cells.items()}


def test_this_months_blocks_are_read_not_other_months():
    r = parse_monthly_pmjdy(make_grid(BLOCKS, rows(A=(15, 2), B=(0, 0))), OCT)
    assert r.month == "2026-10"
    assert r.labels == {"mtd": "MTD Achievement October'26", "ftd": "FTD Achievement October'26",
                        "target": "Targets Oct 2026"}
    # target comes from the October Targets block (222), never the September one (111)
    assert r.rows == {
        "A": {"mtd": 15, "ftd": 2, "target": 222},
        "B": {"mtd": 0, "ftd": 0, "target": 222},
    }
    assert r.rows_read == 2


def test_the_previous_month_is_chosen_only_when_asked_for():
    r = parse_monthly_pmjdy(make_grid(BLOCKS, rows(A=(15, 2))), dt.date(2026, 9, 20))
    assert r.labels == {"mtd": "MTD Achievement September'26", "target": "Targets Sep 2026"}
    assert r.rows["A"] == {"mtd": 333, "ftd": None, "target": 111}  # September's own columns


def test_a_missing_month_block_is_refused_and_last_months_is_never_reused():
    with pytest.raises(PmjdyValidationError) as e:
        parse_monthly_pmjdy(make_grid(BLOCKS, rows(A=(1, 1))), dt.date(2026, 11, 1))
    msg = str(e.value)
    assert "No 'MTD Achievement' block for 2026-11" in msg and "never reused" in msg
    assert "October'26" in msg  # says what it did find


def test_blank_and_bad_cells_are_no_reading_never_zero():
    g = make_grid(BLOCKS, rows(A=(5, 1)))
    g.append(["C", "1"] + [""] * (len(g[0]) - 2))
    g.append(["D", "1"] + ["x"] * (len(g[0]) - 2))
    g.append(["E", "1"] + ["-4"] * (len(g[0]) - 2))
    r = parse_monthly_pmjdy(g, OCT)
    assert r.rows["C"] == {"mtd": None, "ftd": None, "target": None}
    assert r.rows["D"]["mtd"] is None and r.rows["E"]["mtd"] is None
    assert r.rows["A"]["mtd"] == 5


def test_blank_codes_skipped_and_conflicting_duplicates_dropped():
    g = make_grid(BLOCKS, rows(A=(5, 1)))
    g.append(["", "1"] + ["9"] * (len(g[0]) - 2))
    g.append(list(g[2]))  # A again, identical -> fine
    dup = list(g[2])
    dup[-2] = "99"  # A again, different FTD -> ambiguous
    g.append(dup)
    r = parse_monthly_pmjdy(g, OCT)
    assert r.skipped_blank_code == 1 and r.conflicting_codes == ["A"] and "A" not in r.rows


def test_ambiguity_and_wrong_shape_are_errors():
    twice = BLOCKS + [("MTD Achievement Oct 2026", ["PMJDY"])]
    with pytest.raises(PmjdyValidationError, match="Ambiguous"):
        parse_monthly_pmjdy(make_grid(twice, rows(A=(1, 1))), OCT)
    two_cols = [("MTD Achievement October'26", ["PMJDY", "PMJDY"])]
    with pytest.raises(PmjdyValidationError, match="exactly 1"):
        parse_monthly_pmjdy(make_grid(two_cols, {"A": [[1, 2]]}), OCT)
    with pytest.raises(PmjdyValidationError, match="not found"):
        parse_monthly_pmjdy([["", ""], ["Code", "Total"], ["A", "1"]], OCT)
    with pytest.raises(PmjdyValidationError, match="fewer"):
        parse_monthly_pmjdy([["a"]], OCT)


def test_a_label_with_another_year_does_not_match_this_year():
    old = [("MTD Achievement October'25", ["PMJDY"]), ("MTD Achievement October'26", ["PMJDY"])]
    r = parse_monthly_pmjdy(make_grid(old, {"A": [[3], [8]]}), OCT)
    assert r.rows["A"]["mtd"] == 8


def test_optional_blocks_may_be_absent():
    only_mtd = [("MTD Achievement October'26", ["PMJDY"])]
    r = parse_monthly_pmjdy(make_grid(only_mtd, {"A": [[6]]}), OCT)
    assert r.rows["A"] == {"mtd": 6, "ftd": None, "target": None}


@pytest.mark.parametrize("label, expected", [
    ("MTD Achievement October'26", (2026, 10)), ("Targets Oct 2026", (2026, 10)),
    ("FTD Achievement September'26", (2026, 9)), ("Targets Oct", (None, 10)),
    ("Targets Sept 2026", (2026, 9)), ("Loan Lead Generation Oct'26", (2026, 10)),
    ("Slab", None), ("Passbook Printer Status", None), ("", None),
])
def test_month_is_read_from_the_section_label(label, expected):
    assert label_month(label) == expected
