{{ config(location=var('lake') ~ '/gold/schedule_match_rate/data.parquet') }}

-- Backtest of the inferred timetable. For each of the latest `schedule_backtest_months` months,
-- infer a schedule from earlier data only, then grade it against that month's real flights:
--   matched          the flight's (carrier, number, route) is in the timetable
--   times_correct    and both departure and arrival are within `schedule_tolerance_min` minutes
--   times_within_15  both within 15 minutes (close enough to be useful, not exact)
--
-- Two staleness levels:
--   lag_months = 0  timetable from the 60 days right before the month (best case)
--   lag_months = 2  timetable from the 60 days ending 2 months earlier: what live tracking
--                   really has, because BTS publishes about 2 months late
with flights as (
    select *, date_trunc('month', flight_date)::date as month_start
    from {{ ref('int_scheduled_flights') }}
),

tests as (
    select
        m.month_start as test_month,
        l.lag_months,
        (m.month_start - to_months(l.lag_months))::date as window_end
    from (
        select distinct month_start
        from flights
        order by month_start desc
        limit {{ var('schedule_backtest_months') }}
    ) as m
    cross join (values (0), (2)) as l (lag_months)
),

window_flights as (
    select t.test_month, t.lag_months, f.*
    from tests as t
    join flights as f
        on f.flight_date >= t.window_end - to_days({{ var('schedule_window_days') }})
        and f.flight_date < t.window_end
),

schedules as (
    {{ infer_schedule('window_flights', partition_by=['test_month', 'lag_months']) }}
),

graded as (
    select
        t.test_month,
        t.lag_months,
        f.carrier,
        s.carrier is not null as matched,
        greatest(
            {{ minutes_apart('f.dep_minute_local', 's.dep_minute_local') }},
            {{ minutes_apart('f.arr_minute_local', 's.arr_minute_local') }}
        ) as minutes_off
    from tests as t
    join flights as f on f.month_start = t.test_month
    left join schedules as s
        on s.test_month = t.test_month
        and s.lag_months = t.lag_months
        and s.carrier = f.carrier
        and s.flight_number = f.flight_number
        and s.origin = f.origin
        and s.dest = f.dest
)

select
    test_month,
    lag_months,
    carrier,
    count(*) as flights,
    count(*) filter (where matched) as matched,
    count(*) filter (where minutes_off <= {{ var('schedule_tolerance_min') }}) as times_correct,
    count(*) filter (where minutes_off <= 15) as times_within_15
from graded
group by all
order by test_month, lag_months, carrier
