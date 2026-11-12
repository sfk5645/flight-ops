{#
    Past delay statistics per key, as they could be known when a flight in `month_index` is
    predicted: only BTS months published by then, i.e. months up to `bts_lag_months` before.

    Live, BTS is that far behind (on Nov 11 the newest month is August), so training on fresher
    history would teach the model to rely on numbers it never gets at prediction time.

    One row per key and target month_index, with:
      n_3m               flights in the latest 3 published months
      avg_1m / avg_3m    mean delay in the latest 1 / 3 published months
      late15_3m          share of those flights more than 15 minutes late (latest 3 months)

    `flights` must have the key columns, month_index (year * 12 + month - 1), and the delay
    column; `targets` lists the (key, month_index) pairs to compute.
#}
{% macro delay_history(flights, targets, keys, delay) %}
{%- set lag = var('bts_lag_months') -%}
select
    {% for k in keys %}t.{{ k }}, {% endfor %}t.month_index,
    sum(s.n) as n_3m,
    sum(s.total) filter (where s.month_index = t.month_index - {{ lag }})
        / sum(s.n) filter (where s.month_index = t.month_index - {{ lag }}) as avg_1m,
    sum(s.total) / sum(s.n) as avg_3m,
    sum(s.late15) / sum(s.n) as late15_3m
from (select distinct {{ keys | join(', ') }}, month_index from {{ targets }}) as t
join (
    select
        {{ keys | join(', ') }},
        month_index,
        count({{ delay }}) as n,
        sum({{ delay }}) as total,
        count_if({{ delay }} > 15) as late15
    from {{ flights }}
    group by all
) as s
    on {% for k in keys %}s.{{ k }} = t.{{ k }} and {% endfor %}
    s.month_index between t.month_index - {{ lag }} - 2 and t.month_index - {{ lag }}
group by all
{% endmacro %}
