-- Thin pass-through over the Django-owned csp table.
select
    csp_code,
    name,
    account_count,
    status
from {{ source('django', 'csp') }}
