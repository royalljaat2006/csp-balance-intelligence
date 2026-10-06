-- Singular dbt test: a CSP must have at most one daily_activity row per day.
-- Passes when this returns zero rows.
select csp_code, activity_date, count(*) as n
from {{ ref('daily_activity') }}
group by csp_code, activity_date
having count(*) > 1
