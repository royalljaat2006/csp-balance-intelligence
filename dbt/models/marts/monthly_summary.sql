-- Per-CSP, per-month MAB / slab / gap / MoM / trend — PRD §5.2, §8 FR4.
--
-- Deterministic fields only. `projected_mab`, `projected_slab`,
-- `projected_incentive_annual` are NOT computed here — they're a forward
-- estimate, not an aggregate of rows that exist, and live in
-- app/csp/projection.py instead (see that module's docstring). The
-- `sync_monthly_summary` management command reads this mart, calls
-- projection.py, and writes the combined result into the Django-owned
-- csp_monthlysummary table that the API actually serves.
{{ config(materialized='table') }}

with daily as (
    select * from {{ ref('stg_daily_balance') }}
),

csps as (
    select * from {{ ref('stg_csp') }}
),

monthly as (
    select
        csp_code,
        to_char(balance_date, 'YYYY-MM') as month,
        date_trunc('month', balance_date)::date as month_start,
        count(*) as days_with_data,
        avg(daily_avg_balance) as mtd_mab
    from daily
    group by csp_code, to_char(balance_date, 'YYYY-MM'), date_trunc('month', balance_date)::date
),

-- Trend: last-7-known-days average vs the 7 known days before that,
-- *within the same month* (PRD §8: "last 7 days"). Ranking by recency
-- rather than fixed calendar dates so it still works with sparse data.
ranked as (
    select
        csp_code,
        to_char(balance_date, 'YYYY-MM') as month,
        daily_avg_balance,
        row_number() over (
            partition by csp_code, to_char(balance_date, 'YYYY-MM')
            order by balance_date desc
        ) as rn_desc
    from daily
),

trend as (
    select
        csp_code,
        month,
        avg(daily_avg_balance) filter (where rn_desc <= 7) as last_7d_avg,
        avg(daily_avg_balance) filter (where rn_desc > 7 and rn_desc <= 14) as prior_7d_avg
    from ranked
    group by csp_code, month
),

with_prev_month as (
    select
        m.*,
        lag(m.mtd_mab) over (partition by m.csp_code order by m.month_start) as prev_month_mab
    from monthly m
)

select
    w.csp_code,
    w.month,
    extract(day from (w.month_start + interval '1 month - 1 day'))::int as days_in_month,
    w.days_with_data,
    round(w.mtd_mab, 2) as mtd_mab,
    {{ rule19_slab('w.mtd_mab', 'c.account_count') }} as slab,
    {{ rule19_rate('w.mtd_mab', 'c.account_count') }} as incentive_rate_pa,
    round({{ rule19_gap_to_min('w.mtd_mab') }}, 2) as gap_to_min,
    round({{ rule19_gap_to_next_slab('w.mtd_mab', 'c.account_count') }}, 2) as gap_to_next_slab,
    round(w.prev_month_mab, 2) as prev_month_mab,
    round(w.mtd_mab - w.prev_month_mab, 2) as mom_change_abs,
    case
        when w.prev_month_mab is not null and w.prev_month_mab != 0
        then round((w.mtd_mab - w.prev_month_mab) / w.prev_month_mab * 100, 2)
    end as mom_change_pct,
    case
        when t.prior_7d_avg is not null and t.prior_7d_avg != 0
        then round((t.last_7d_avg - t.prior_7d_avg) / t.prior_7d_avg * 100, 2)
    end as trend_7d_pct,
    coalesce(c.account_count, 0) >= 200 as is_eligible,
    c.account_count
from with_prev_month w
left join trend t on t.csp_code = w.csp_code and t.month = w.month
left join csps c on c.csp_code = w.csp_code
