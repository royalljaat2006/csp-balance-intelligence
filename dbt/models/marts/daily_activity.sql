-- Per-CSP per-day activity rollup — PRD §7.2/§10, extended 2026-09-18 for
-- the ONUS/Overall analytical split. The unprefixed columns (txn_count,
-- txn_amount, ...) are "Overall" mode — the full 7-type approved allow-list,
-- unchanged from before this split existed, so every existing consumer of
-- this mart (Trends page, API) is already reading Overall-mode numbers
-- without needing a single change. The onus_-prefixed columns are the
-- narrower 4-type on-us subset (see stg_transaction.sql's is_onus).
-- Not an input to MAB either way — descriptive context alongside balance.
{{ config(materialized='table') }}

with txns as (
    select * from {{ ref('stg_transaction') }}
)

select
    csp_code,
    txn_date as activity_date,

    -- Overall (full approved allow-list) — pre-existing, unchanged.
    count(*) as txn_count,
    sum(amount) as txn_amount,
    count(*) filter (where category = 'withdrawal') as withdrawal_count,
    coalesce(sum(amount) filter (where category = 'withdrawal'), 0) as withdrawal_amount,
    count(*) filter (where category = 'deposit') as deposit_count,
    coalesce(sum(amount) filter (where category = 'deposit'), 0) as deposit_amount,
    count(*) filter (where category = 'fund_transfer') as fund_transfer_count,
    coalesce(sum(amount) filter (where category = 'fund_transfer'), 0) as fund_transfer_amount,
    coalesce(sum(amount) filter (where direction = 'in_pool'), 0) as cash_in_pool,
    coalesce(sum(amount) filter (where direction = 'out_pool'), 0) as cash_out_pool,
    coalesce(sum(amount) filter (where direction = 'in_pool'), 0)
        - coalesce(sum(amount) filter (where direction = 'out_pool'), 0) as net_flow,

    -- ONUS (narrower on-us subset) — new.
    count(*) filter (where is_onus) as onus_txn_count,
    coalesce(sum(amount) filter (where is_onus), 0) as onus_txn_amount,
    count(*) filter (where is_onus and category = 'withdrawal') as onus_withdrawal_count,
    coalesce(sum(amount) filter (where is_onus and category = 'withdrawal'), 0)
        as onus_withdrawal_amount,
    count(*) filter (where is_onus and category = 'deposit') as onus_deposit_count,
    coalesce(sum(amount) filter (where is_onus and category = 'deposit'), 0)
        as onus_deposit_amount,
    coalesce(sum(amount) filter (where is_onus and direction = 'in_pool'), 0)
        as onus_cash_in_pool,
    coalesce(sum(amount) filter (where is_onus and direction = 'out_pool'), 0)
        as onus_cash_out_pool,
    coalesce(sum(amount) filter (where is_onus and direction = 'in_pool'), 0)
        - coalesce(sum(amount) filter (where is_onus and direction = 'out_pool'), 0)
        as onus_net_flow
from txns
group by csp_code, txn_date
