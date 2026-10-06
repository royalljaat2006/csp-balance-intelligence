-- gap_to_min is "how much more to reach ₹2,501" — never negative (PRD §5.1).
-- Passes when this returns zero rows.
select csp_code, month, mtd_mab, gap_to_min
from {{ ref('monthly_summary') }}
where gap_to_min < 0
