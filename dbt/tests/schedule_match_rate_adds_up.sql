-- Each backtest grades every flight of its month exactly once; correct (5 min) implies within
-- 15 min, which implies a match.
with month_flights as (
    select make_date(year, month, 1) as test_month, count(*) as flights
    from {{ ref('fct_flights_hist') }}
    group by all
),
graded as (
    select test_month, lag_months, sum(flights) as flights, sum(matched) as matched,
        sum(times_correct) as times_correct, sum(times_within_15) as times_within_15
    from {{ ref('schedule_match_rate') }}
    group by all
)
select g.*, m.flights as expected_flights
from graded as g
left join month_flights as m using (test_month)
where m.flights is distinct from g.flights
   or g.times_correct > g.times_within_15
   or g.times_within_15 > g.matched
