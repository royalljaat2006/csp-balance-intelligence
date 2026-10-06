-- Singular dbt test (no dbt_utils dependency): a CSP must have at most one
-- monthly_summary row per month. Passes when this returns zero rows.
select csp_code, month, count(*) as n
from {{ ref('monthly_summary') }}
group by csp_code, month
having count(*) > 1
