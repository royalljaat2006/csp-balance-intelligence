{#
  Rule #19 slab thresholds — PRD §5.1. Mirrors app/csp/rules.py exactly
  (same band boundaries, same rates, same caps). If you change a boundary
  here, change it there too — tests/unit/test_rules.py is the tripwire on
  the Python side; there is no automated cross-check against this SQL copy,
  so review both files together.
#}

{#
  account_count_column gates all three on PRD §5.1's "Eligibility gate:
  minimum 200 BSBD accounts" -- a CSP under that threshold (or with a
  null/unreported count, same convention as csp/rules.py's is_eligible)
  is NIL regardless of balance, not merely flagged separately. Mirrors
  csp/rules.py's slab_for/gap_to_next_slab exactly -- keep both in sync.
#}

{% macro rule19_slab(mab_column, account_count_column) %}
    case
        when coalesce({{ account_count_column }}, 0) < 200 then 'NIL'
        when {{ mab_column }} <= 2500 then 'NIL'
        when {{ mab_column }} <= 4000 then 'S1'
        when {{ mab_column }} <= 6000 then 'S2'
        when {{ mab_column }} <= 10000 then 'S3'
        else 'S4'
    end
{% endmacro %}

{% macro rule19_rate(mab_column, account_count_column) %}
    case
        when coalesce({{ account_count_column }}, 0) < 200 then 0
        when {{ mab_column }} <= 2500 then 0
        when {{ mab_column }} <= 4000 then 1.10
        when {{ mab_column }} <= 6000 then 1.20
        when {{ mab_column }} <= 10000 then 1.25
        else 1.30
    end
{% endmacro %}

{% macro rule19_gap_to_min(mab_column) %}
    greatest(2501 - {{ mab_column }}, 0)
{% endmacro %}

{% macro rule19_gap_to_next_slab(mab_column, account_count_column) %}
    case
        -- Ineligible -> same gap a genuine NIL CSP sees (balance needed to
        -- clear the floor, floored at 0), not a gap computed from whatever
        -- band their real MAB falls in -- that would contradict the NIL
        -- slab they're actually shown.
        when coalesce({{ account_count_column }}, 0) < 200
            then greatest(2501 - {{ mab_column }}, 0)
        when {{ mab_column }} <= 2500 then 2501 - {{ mab_column }}
        when {{ mab_column }} <= 4000 then 4001 - {{ mab_column }}
        when {{ mab_column }} <= 6000 then 6001 - {{ mab_column }}
        when {{ mab_column }} <= 10000 then 10001 - {{ mab_column }}
        else null
    end
{% endmacro %}
