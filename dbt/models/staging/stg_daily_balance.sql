-- Thin pass-through over the Django-owned table. Proves the dbt -> Postgres
-- wiring; the real transformation work (MTD MAB, projection, slab, trend —
-- PRD FR4) lands in models/marts/monthly_summary.sql, not yet implemented.
select
    csp_id as csp_code,
    balance_date,
    daily_avg_balance,
    source
from {{ source('django', 'daily_balance') }}
