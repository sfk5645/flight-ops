{{ config(location=var('lake') ~ '/gold/dim_schedule/data.parquet') }}

-- The inferred timetable: one row per (carrier, flight number, route), from the latest
-- `schedule_window_days` of BTS. The web app uses it only to pre-fill a flight's scheduled times
-- as a suggestion; the user confirms or corrects them. It is never trusted silently: BTS is
-- about 2 months behind, and schedule_match_rate shows how often a timetable that old is right.
with bounds as (
    select
        max(flight_date) as window_end,
        max(flight_date) - to_days({{ var('schedule_window_days') }} - 1) as window_start
    from {{ ref('int_scheduled_flights') }}
),

window_flights as (
    select f.*
    from {{ ref('int_scheduled_flights') }} as f, bounds as b
    where f.flight_date between b.window_start and b.window_end
),

schedule as (
    {{ infer_schedule('window_flights') }}
)

select
    s.carrier,
    s.flight_number,
    s.origin,
    s.dest,
    s.origin || '-' || s.dest as route,
    make_time(s.dep_minute_local // 60, s.dep_minute_local % 60, 0) as sched_dep_time_local,
    make_time(s.arr_minute_local // 60, s.arr_minute_local % 60, 0) as sched_arr_time_local,
    s.arr_day_offset,
    s.first_seen_date as time_first_seen,
    s.last_seen_date as time_last_seen,
    s.times_seen,
    s.times_scheduled,
    b.window_start::date as window_start,
    b.window_end
from schedule as s, bounds as b
order by s.carrier, s.flight_number, s.origin, s.dest
