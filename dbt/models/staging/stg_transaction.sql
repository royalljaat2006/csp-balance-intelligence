-- Thin pass-through over the Django-owned transaction table. Already
-- allow-list-filtered at ingestion time (PRD §7.2) — every row here is one
-- of the 7 in-scope types ("Overall" mode, confirmed 2026-09-18: the full
-- approved allow-list, not "every type in the source file").
--
-- is_onus: the narrower 4-type on-us-channel subset within that same
-- approved list (AEPS/ATM types with "ONUS"/"Onus" literally in the name).
-- Mirrors ingestion/xlsx_validation.py's ONUS_TXN_TYPES — the two must be
-- kept in sync if the approved list ever changes, same convention as
-- macros/rule_19.sql already mirroring csp/rules.py.
select
    ref_number,
    csp_id as csp_code,
    txn_datetime,
    txn_date,
    txn_type,
    category,
    direction,
    amount,
    txn_type in (
        'AEPS ONUS Withdrawal', 'ATM Onus Withdrawal',
        'AEPS ONUS Deposit', 'AEPS ONUS Fund Transfer'
    ) as is_onus
from {{ source('django', 'transaction') }}
