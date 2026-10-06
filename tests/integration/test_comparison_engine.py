"""
csp.comparison — the canonical historical-comparison engine. These tests
are the tripwire for the non-negotiable rules the spec called out
explicitly: missing data must never read as DECLINE, month-end fallback
must be explicit (never an arbitrary nearby date), and ONUS/Overall must
never cross-contaminate.
"""

import datetime as dt
import decimal

import pytest
from csp import comparison as cmp
from csp.models import Csp, DailyBalance, DailyCspSnapshot, MonthlySummary

# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------


def test_same_date_previous_month_normal_case():
    assert cmp.resolve_same_date_previous_month(dt.date(2026, 9, 27)) == dt.date(2026, 8, 27)


def test_same_date_previous_month_clamps_at_month_end_by_default():
    # 31 Mar -> Feb has no 31st (2026 isn't a leap year) -> clamp to 28.
    assert cmp.resolve_same_date_previous_month(dt.date(2026, 3, 31)) == dt.date(2026, 2, 28)


def test_same_date_previous_month_leap_february():
    # 2028 is a leap year -> 29 Feb exists.
    assert cmp.resolve_same_date_previous_month(dt.date(2028, 3, 31)) == dt.date(2028, 2, 29)


def test_same_date_previous_month_fallback_none_returns_none():
    result = cmp.resolve_same_date_previous_month(dt.date(2026, 3, 31), fallback=cmp.FALLBACK_NONE)
    assert result is None


def test_same_date_previous_month_crosses_year_boundary():
    assert cmp.resolve_same_date_previous_month(dt.date(2026, 1, 15)) == dt.date(2025, 12, 15)


def test_resolve_comparison_date_defaults_to_yesterday():
    assert cmp.resolve_comparison_date(dt.date(2026, 9, 27)) == dt.date(2026, 9, 26)


def test_resolve_comparison_date_same_date_previous_month():
    result = cmp.resolve_comparison_date(
        dt.date(2026, 9, 27), comparison_type=cmp.COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH
    )
    assert result == dt.date(2026, 8, 27)


def test_resolve_comparison_date_same_date_previous_month_respects_fallback_none():
    result = cmp.resolve_comparison_date(
        dt.date(2026, 3, 31),
        comparison_type=cmp.COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH,
        fallback=cmp.FALLBACK_NONE,
    )
    assert result is None


def test_resolve_comparison_date_explicit_date_wins_over_comparison_type():
    explicit = dt.date(2026, 1, 1)
    result = cmp.resolve_comparison_date(
        dt.date(2026, 9, 27),
        comparison_type=cmp.COMPARISON_TYPE_SAME_DATE_PREVIOUS_MONTH,
        explicit_comparison_date=explicit,
    )
    assert result == explicit


# ---------------------------------------------------------------------------
# Trend / movement classification — the "missing data != decline" rule
# ---------------------------------------------------------------------------


def test_classify_trend_growth_decline_no_change():
    d = decimal.Decimal
    assert cmp.classify_trend(d("100"), d("90")) == cmp.Trend.GROWTH
    assert cmp.classify_trend(d("90"), d("100")) == cmp.Trend.DECLINE
    assert cmp.classify_trend(d("100"), d("100")) == cmp.Trend.NO_CHANGE


def test_classify_trend_missing_data_is_never_decline():
    d = decimal.Decimal
    assert cmp.classify_trend(None, d("100")) == cmp.Trend.NO_DATA
    assert cmp.classify_trend(d("100"), None) == cmp.Trend.NO_DATA
    assert cmp.classify_trend(None, None) == cmp.Trend.NO_DATA


def test_pct_change_zero_previous_is_none_not_infinite():
    assert cmp.pct_change(decimal.Decimal("100"), decimal.Decimal("0")) is None


def test_pct_change_missing_value_is_none():
    assert cmp.pct_change(None, decimal.Decimal("100")) is None


def test_classify_movement_buckets():
    d = decimal.Decimal
    assert cmp.classify_movement(d("15")) == cmp.MovementBucket.STRONG_GROWTH
    assert cmp.classify_movement(d("5")) == cmp.MovementBucket.MODERATE_GROWTH
    assert cmp.classify_movement(d("0")) == cmp.MovementBucket.STABLE
    assert cmp.classify_movement(d("-5")) == cmp.MovementBucket.MODERATE_DECLINE
    assert cmp.classify_movement(d("-15")) == cmp.MovementBucket.SHARP_DECLINE
    assert cmp.classify_movement(None) == cmp.MovementBucket.NO_DATA


def test_comparison_result_never_treats_missing_as_decline():
    result = cmp.ComparisonResult(dt.date(2026, 9, 27), None, decimal.Decimal("5000"), None)
    assert result.trend == cmp.Trend.NO_DATA
    assert result.movement == cmp.MovementBucket.NO_DATA
    assert result.abs_change is None


# ---------------------------------------------------------------------------
# compare_csp_metrics — needs real DB rows
# ---------------------------------------------------------------------------


@pytest.fixture
def csp_with_history(db):
    csp = Csp.objects.create(csp_code="1A850001", name="Test CSP", account_count=300)
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 26), daily_avg_balance=decimal.Decimal("5000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 27), daily_avg_balance=decimal.Decimal("5500.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    return csp


@pytest.mark.django_db
def test_compare_csp_metrics_today_vs_yesterday(csp_with_history):
    result = cmp.compare_csp_metrics(
        csp_with_history.csp_code, dt.date(2026, 9, 27), dt.date(2026, 9, 26)
    )
    assert result.balance.current_value == decimal.Decimal("5500.00")
    assert result.balance.previous_value == decimal.Decimal("5000.00")
    assert result.balance.trend == cmp.Trend.GROWTH


@pytest.mark.django_db
def test_compare_csp_metrics_missing_yesterday_is_no_data_not_decline(csp_with_history):
    # No DailyBalance row for 2026-09-25 at all.
    result = cmp.compare_csp_metrics(
        csp_with_history.csp_code, dt.date(2026, 9, 26), dt.date(2026, 9, 25)
    )
    assert result.balance.previous_value is None
    assert result.balance.trend == cmp.Trend.NO_DATA


@pytest.mark.django_db
def test_compare_csp_metrics_rejects_bad_mode(csp_with_history):
    with pytest.raises(ValueError):
        cmp.compare_csp_metrics(
            csp_with_history.csp_code, dt.date(2026, 9, 27), dt.date(2026, 9, 26), mode="bogus"
        )


@pytest.mark.django_db
def test_bulk_compare_csps_matches_single_csp_result(csp_with_history):
    bulk = cmp.bulk_compare_csps(
        dt.date(2026, 9, 27), dt.date(2026, 9, 26), csp_codes=[csp_with_history.csp_code]
    )
    single = cmp.compare_csp_metrics(
        csp_with_history.csp_code, dt.date(2026, 9, 27), dt.date(2026, 9, 26)
    )
    assert len(bulk) == 1
    assert bulk[0].balance.current_value == single.balance.current_value
    assert bulk[0].balance.trend == single.balance.trend


@pytest.mark.django_db
def test_resolve_matching_csp_codes_returns_none_without_filters():
    assert cmp.resolve_matching_csp_codes(dt.date(2026, 9, 27)) is None


@pytest.mark.django_db
def test_resolve_matching_csp_codes_by_slab():
    csp = Csp.objects.create(csp_code="1A850090", name="Slab CSP")
    DailyCspSnapshot.objects.create(
        csp=csp, business_date=dt.date(2026, 9, 27), mtd_avg_so_far=decimal.Decimal("5000"),
        slab=MonthlySummary.Slab.S3,
    )
    matches = cmp.resolve_matching_csp_codes(dt.date(2026, 9, 27), slab="S3")
    assert matches == [csp.csp_code]
    assert cmp.resolve_matching_csp_codes(dt.date(2026, 9, 27), slab="S4") == []


@pytest.mark.django_db
def test_resolve_matching_csp_codes_by_search_matches_code_or_name():
    Csp.objects.create(csp_code="1A850091", name="Findme Traders")
    Csp.objects.create(csp_code="1A850092", name="Other")
    assert cmp.resolve_matching_csp_codes(dt.date(2026, 9, 27), search="findme") == ["1A850091"]
    assert cmp.resolve_matching_csp_codes(dt.date(2026, 9, 27), search="1A850092") == ["1A850092"]


# ---------------------------------------------------------------------------
# Rolling averages
# ---------------------------------------------------------------------------


@pytest.fixture
def csp_with_two_weeks(db):
    csp = Csp.objects.create(csp_code="1A850002", name="Two Week CSP")
    # Week 1 (older): avg 4000. Week 2 (recent): avg 5000.
    for i, val in enumerate([4000] * 7 + [5000] * 7):
        DailyBalance.objects.create(
            csp=csp, balance_date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
            daily_avg_balance=decimal.Decimal(val), source=DailyBalance.Source.CALLING_SHEET,
        )
    return csp


@pytest.mark.django_db
def test_rolling_balance_average(csp_with_two_weeks):
    avg = cmp.get_rolling_balance_average(
        csp_with_two_weeks.csp_code, dt.date(2026, 9, 14), window_days=7
    )
    assert avg == decimal.Decimal("5000.00")


@pytest.mark.django_db
def test_compare_rolling_balance_shows_growth(csp_with_two_weeks):
    result = cmp.compare_rolling_balance(
        csp_with_two_weeks.csp_code, dt.date(2026, 9, 14), window_days=7
    )
    assert result.current_value == decimal.Decimal("5000.00")
    assert result.previous_value == decimal.Decimal("4000.00")
    assert result.trend == cmp.Trend.GROWTH


# ---------------------------------------------------------------------------
# Trend intelligence — streaks and reversal
# ---------------------------------------------------------------------------


@pytest.fixture
def csp_with_decline_then_growth(db):
    csp = Csp.objects.create(csp_code="1A850003", name="Reversal CSP")
    # Declining for 3 days, then growing for 2.
    values = [5000, 4800, 4600, 4400, 4600, 4900]
    for i, val in enumerate(values):
        DailyBalance.objects.create(
            csp=csp, balance_date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
            daily_avg_balance=decimal.Decimal(val), source=DailyBalance.Source.CALLING_SHEET,
        )
    return csp


@pytest.mark.django_db
def test_trend_intelligence_detects_reversal(csp_with_decline_then_growth):
    result = cmp.get_trend_intelligence(csp_with_decline_then_growth.csp_code, dt.date(2026, 9, 6))
    assert result.trend_reversal == cmp.TrendReversal.DECLINE_TO_GROWTH
    assert result.consecutive_growth_days == 2


@pytest.mark.django_db
def test_trend_intelligence_consecutive_decline_days(csp_with_decline_then_growth):
    result = cmp.get_trend_intelligence(csp_with_decline_then_growth.csp_code, dt.date(2026, 9, 4))
    assert result.consecutive_decline_days == 3
    assert result.consecutive_growth_days == 0


@pytest.mark.django_db
def test_trend_intelligence_empty_history_returns_no_crash(db):
    csp = Csp.objects.create(csp_code="1A850099", name="No History")
    result = cmp.get_trend_intelligence(csp.csp_code, dt.date(2026, 9, 27))
    assert result.current_value is None
    assert result.consecutive_growth_days == 0
    assert result.trend_reversal == cmp.TrendReversal.NONE


# ---------------------------------------------------------------------------
# build_daily_snapshot — the daily-slab computation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_build_daily_snapshot_computes_mtd_and_slab(csp_with_history):
    snap = cmp.build_daily_snapshot(csp_with_history.csp_code, dt.date(2026, 9, 27))
    assert snap is not None
    # MTD-so-far through 27 Sep = avg(5000, 5500) = 5250 -> S2 (4001-6000).
    assert snap.mtd_avg_so_far == decimal.Decimal("5250.00")
    assert snap.slab == MonthlySummary.Slab.S2
    assert snap.days_with_data_mtd == 2
    assert snap.is_eligible is True  # account_count=300 >= 200


@pytest.mark.django_db
def test_build_daily_snapshot_returns_none_without_any_data(db):
    csp = Csp.objects.create(csp_code="1A850098", name="Empty")
    assert cmp.build_daily_snapshot(csp.csp_code, dt.date(2026, 9, 27)) is None
    assert not DailyCspSnapshot.objects.filter(csp_id=csp.csp_code).exists()


@pytest.mark.django_db
def test_build_daily_snapshot_is_idempotent(csp_with_history):
    cmp.build_daily_snapshot(csp_with_history.csp_code, dt.date(2026, 9, 27))
    cmp.build_daily_snapshot(csp_with_history.csp_code, dt.date(2026, 9, 27))
    assert DailyCspSnapshot.objects.filter(
        csp_id=csp_with_history.csp_code, business_date=dt.date(2026, 9, 27)
    ).count() == 1


# ---------------------------------------------------------------------------
# get_csp_slab_history
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_csp_slab_history_returns_snapshots_ascending(db):
    csp = Csp.objects.create(csp_code="1A850099", name="History CSP", account_count=300)
    today = dt.date.today()
    for i, val in enumerate([3000, 3200, 5000]):
        DailyBalance.objects.create(
            csp=csp, balance_date=today - dt.timedelta(days=2 - i),
            daily_avg_balance=decimal.Decimal(val), source=DailyBalance.Source.CALLING_SHEET,
        )
        cmp.build_daily_snapshot(csp.csp_code, today - dt.timedelta(days=2 - i))

    history = cmp.get_csp_slab_history(csp.csp_code, days=10)
    assert [h.business_date for h in history] == [
        today - dt.timedelta(days=2), today - dt.timedelta(days=1), today,
    ]


@pytest.mark.django_db
def test_get_csp_slab_history_empty_without_snapshots(db):
    csp = Csp.objects.create(csp_code="1A850098", name="No History")
    assert cmp.get_csp_slab_history(csp.csp_code) == []


# ---------------------------------------------------------------------------
# build_daily_snapshots_bulk — same math as build_daily_snapshot(), batched
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_build_daily_snapshots_bulk_matches_single_csp_result(csp_with_history):
    single = cmp.build_daily_snapshot(csp_with_history.csp_code, dt.date(2026, 9, 27))
    assert single is not None
    DailyCspSnapshot.objects.all().delete()  # single-CSP call already wrote one row

    bulk = cmp.build_daily_snapshots_bulk(dt.date(2026, 9, 27))
    assert len(bulk) == 1
    assert bulk[0].csp_id == single.csp_id
    assert bulk[0].mtd_avg_so_far == single.mtd_avg_so_far
    assert bulk[0].slab == single.slab
    assert bulk[0].data_status == single.data_status


@pytest.mark.django_db
def test_build_daily_snapshots_bulk_skips_csps_without_data(csp_with_history, db):
    Csp.objects.create(csp_code="1A850097", name="No History This Month")
    bulk = cmp.build_daily_snapshots_bulk(dt.date(2026, 9, 27))
    assert {s.csp_id for s in bulk} == {csp_with_history.csp_code}
    assert not DailyCspSnapshot.objects.filter(csp_id="1A850097").exists()


@pytest.mark.django_db
def test_build_daily_snapshots_bulk_is_idempotent(csp_with_history):
    cmp.build_daily_snapshots_bulk(dt.date(2026, 9, 27))
    cmp.build_daily_snapshots_bulk(dt.date(2026, 9, 27))
    assert DailyCspSnapshot.objects.filter(
        csp_id=csp_with_history.csp_code, business_date=dt.date(2026, 9, 27)
    ).count() == 1


@pytest.mark.django_db
def test_build_daily_snapshots_bulk_respects_csp_codes_filter(csp_with_history, db):
    other = Csp.objects.create(csp_code="1A850096", name="Other")
    DailyBalance.objects.create(
        csp=other, balance_date=dt.date(2026, 9, 27), daily_avg_balance=decimal.Decimal("6000.00"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    bulk = cmp.build_daily_snapshots_bulk(
        dt.date(2026, 9, 27), csp_codes=[csp_with_history.csp_code]
    )
    assert {s.csp_id for s in bulk} == {csp_with_history.csp_code}


# ---------------------------------------------------------------------------
# Slab movement
# ---------------------------------------------------------------------------


def test_slab_movement_detects_upgrade():
    move = cmp.SlabMovement(current_slab="S2", previous_slab="S1")
    assert move.moved is True
    assert move.upgraded is True
    assert move.downgraded is False


def test_slab_movement_detects_downgrade():
    move = cmp.SlabMovement(current_slab="NIL", previous_slab="S3")
    assert move.downgraded is True
    assert move.upgraded is False


def test_slab_movement_no_data_is_not_a_movement():
    move = cmp.SlabMovement(current_slab="S2", previous_slab=None)
    assert move.moved is False
    assert move.upgraded is False
    assert move.downgraded is False


# ---------------------------------------------------------------------------
# filter_and_sort_comparisons — pure, no DB needed (operates on already-
# computed CspComparison objects, built by hand here for speed/precision).
# ---------------------------------------------------------------------------


def _cmp_result(current, previous):
    to_decimal = lambda v: decimal.Decimal(str(v)) if v is not None else None  # noqa: E731
    return cmp.ComparisonResult(
        dt.date(2026, 9, 27), dt.date(2026, 9, 26), to_decimal(current), to_decimal(previous)
    )


def _make_comparison(csp_code, csp_name, *, current, previous, slab="S1"):
    balance = _cmp_result(current, previous)
    zero = _cmp_result(0, 0)
    mtd_avg_so_far = decimal.Decimal(str(current)) if current is not None else None
    return cmp.CspComparison(
        csp_code=csp_code,
        csp_name=csp_name,
        mode="overall",
        current_date=dt.date(2026, 9, 27),
        comparison_date=dt.date(2026, 9, 26),
        balance=balance,
        txn_count=zero,
        txn_amount=zero,
        slab_movement=cmp.SlabMovement(current_slab=slab, previous_slab=slab),
        current_slab=slab,
        previous_slab=slab,
        mtd_avg_so_far=mtd_avg_so_far,
        gap_to_min=None,
        gap_to_next_slab=None,
        current_data_status=cmp.DataStatus.FRESH,
        comparison_data_status=cmp.DataStatus.FRESH,
    )


def test_filter_and_sort_filters_by_trend():
    comparisons = [
        _make_comparison("A1", "Alpha", current=120, previous=100),
        _make_comparison("B1", "Beta", current=90, previous=100),
        _make_comparison("C1", "Gamma", current=None, previous=None),
    ]
    growing = cmp.filter_and_sort_comparisons(comparisons, trend=cmp.Trend.GROWTH)
    assert [c.csp_code for c in growing] == ["A1"]


def test_filter_and_sort_filters_by_slab():
    comparisons = [
        _make_comparison("A1", "Alpha", current=120, previous=100, slab="S2"),
        _make_comparison("B1", "Beta", current=90, previous=100, slab="S1"),
    ]
    result = cmp.filter_and_sort_comparisons(comparisons, slab="S1")
    assert [c.csp_code for c in result] == ["B1"]


def test_filter_and_sort_search_matches_code_or_name():
    comparisons = [
        _make_comparison("1A850001", "Ramesh Kumar", current=100, previous=100),
        _make_comparison("1A850002", "Suresh Babu", current=100, previous=100),
    ]
    by_code = cmp.filter_and_sort_comparisons(comparisons, search="850001")
    by_name = cmp.filter_and_sort_comparisons(comparisons, search="suresh")
    assert [c.csp_code for c in by_code] == ["1A850001"]
    assert [c.csp_code for c in by_name] == ["1A850002"]


def test_filter_and_sort_sorts_descending_by_pct_change_by_default():
    comparisons = [
        _make_comparison("A1", "Alpha", current=105, previous=100),  # +5%
        _make_comparison("B1", "Beta", current=130, previous=100),  # +30%
        _make_comparison("C1", "Gamma", current=90, previous=100),  # -10%
    ]
    result = cmp.filter_and_sort_comparisons(comparisons)
    assert [c.csp_code for c in result] == ["B1", "A1", "C1"]


def test_filter_and_sort_no_data_rows_always_sort_last():
    comparisons = [
        _make_comparison("A1", "Alpha", current=90, previous=100),
        _make_comparison("B1", "Beta", current=None, previous=None),
        _make_comparison("C1", "Gamma", current=110, previous=100),
    ]
    desc = cmp.filter_and_sort_comparisons(comparisons, sort_dir="desc")
    asc = cmp.filter_and_sort_comparisons(comparisons, sort_dir="asc")
    assert desc[-1].csp_code == "B1"
    assert asc[-1].csp_code == "B1"


def test_filter_and_sort_by_csp_code_ascending():
    comparisons = [
        _make_comparison("C1", "Gamma", current=100, previous=100),
        _make_comparison("A1", "Alpha", current=100, previous=100),
        _make_comparison("B1", "Beta", current=100, previous=100),
    ]
    result = cmp.filter_and_sort_comparisons(comparisons, sort_by="csp_code", sort_dir="asc")
    assert [c.csp_code for c in result] == ["A1", "B1", "C1"]


# ---------------------------------------------------------------------------
# summarize_by_slab
# ---------------------------------------------------------------------------


def test_summarize_by_slab_groups_and_averages():
    comparisons = [
        _make_comparison("A1", "Alpha", current=3000, previous=2900, slab="S1"),
        _make_comparison("A2", "Alpha2", current=3500, previous=3600, slab="S1"),
        _make_comparison("B1", "Beta", current=5000, previous=4800, slab="S2"),
    ]
    summaries = cmp.summarize_by_slab(comparisons)
    by_slab = {s.slab: s for s in summaries}

    assert [s.slab for s in summaries] == ["NIL", "S1", "S2", "S3", "S4"]
    assert by_slab["S1"].count == 2
    assert by_slab["S1"].avg_balance == decimal.Decimal("3250.00")
    assert by_slab["S1"].growing_count == 1
    assert by_slab["S1"].declining_count == 1
    assert by_slab["S2"].count == 1
    assert by_slab["S2"].growing_count == 1
    assert by_slab["NIL"].count == 0
    assert by_slab["NIL"].avg_balance is None


def test_summarize_by_slab_ignores_no_slab_comparisons():
    comparisons = [_make_comparison("A1", "Alpha", current=None, previous=None, slab=None)]
    summaries = cmp.summarize_by_slab(comparisons)
    assert all(s.count == 0 for s in summaries)


# ---------------------------------------------------------------------------
# Top movers / at-risk
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_top_movers_growth_and_decline(csp_with_history):
    comparisons = cmp.bulk_compare_csps(dt.date(2026, 9, 27), dt.date(2026, 9, 26))
    growth = cmp.top_movers(comparisons, by="pct", direction="growth", limit=5)
    decline = cmp.top_movers(comparisons, by="pct", direction="decline", limit=5)
    assert len(growth) == 1  # the one CSP grew
    assert len(decline) == 0


@pytest.mark.django_db
def test_at_risk_flags_are_explainable(db):
    csp = Csp.objects.create(csp_code="1A850004", name="Risky CSP", account_count=250)
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 26), daily_avg_balance=decimal.Decimal("2540"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    DailyBalance.objects.create(
        csp=csp, balance_date=dt.date(2026, 9, 27), daily_avg_balance=decimal.Decimal("2510"),
        source=DailyBalance.Source.CALLING_SHEET,
    )
    # MTD-so-far through 27 Sep = avg(2540, 2510) = 2525.00, i.e. Rs.24 above
    # MIN_BALANCE (2501) — comfortably inside near_threshold=50 below.
    cmp.build_daily_snapshot(csp.csp_code, dt.date(2026, 9, 27))
    comparisons = cmp.bulk_compare_csps(
        dt.date(2026, 9, 27), dt.date(2026, 9, 26), csp_codes=[csp.csp_code]
    )
    flags = cmp.at_risk_csps(comparisons, near_threshold=decimal.Decimal("50"))
    assert len(flags) == 1
    assert any("above the eligibility minimum" in r for r in flags[0].reasons)
