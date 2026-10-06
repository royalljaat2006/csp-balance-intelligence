{#
  Rule #19 slab thresholds — PRD §5.1. Mirrors app/csp/rules.py exactly
  (same band boundaries, same rates, same caps). If you change a boundary
  here, change it there too — tests/unit/test_rules.py is the tripwire on
  the Python side; there is no automated cross-check against this SQL copy,
  so review both files together.
#}

{% macro rule19_slab(mab_column) %}
    case
        when {{ mab_column }} <= 2500 then 'NIL'
        when {{ mab_column }} <= 4000 then 'S1'
        when {{ mab_column }} <= 6000 then 'S2'
        when {{ mab_column }} <= 10000 then 'S3'
        else 'S4'
    end
{% endmacro %}

{% macro rule19_rate(mab_column) %}
    case
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

{% macro rule19_gap_to_next_slab(mab_column) %}
    case
        when {{ mab_column }} <= 2500 then 2501 - {{ mab_column }}
        when {{ mab_column }} <= 4000 then 4001 - {{ mab_column }}
        when {{ mab_column }} <= 6000 then 6001 - {{ mab_column }}
        when {{ mab_column }} <= 10000 then 10001 - {{ mab_column }}
        else null
    end
{% endmacro %}
