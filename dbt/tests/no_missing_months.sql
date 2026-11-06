-- Every month between the first and last loaded month must be present, and look complete
-- (a normal month has 500k+ flights; a partial load would show far fewer).
with loaded as (
    select make_date(year, month, 1) as month_start, count(*) as flights
    from {{ ref('fct_flights_hist') }}
    group by all
),
expected as (
    select unnest(generate_series(min(month_start), max(month_start), interval 1 month))::date
        as month_start
    from loaded
)
select e.month_start, l.flights
from expected as e
left join loaded as l using (month_start)
where l.flights is null or l.flights < 400000
