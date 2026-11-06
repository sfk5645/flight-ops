-- Every scheduled flight (including cancelled ones: they were still on the timetable) as local
-- clock times, which is how airlines publish schedules. "08:05 every Monday" stays 08:05 across a
-- daylight saving change even though its UTC time moves by an hour.
with local_times as (
    select
        carrier,
        flight_number,
        origin,
        dest,
        flight_date,
        sched_dep_weekday_local as weekday,
        sched_dep_local,
        timezone(dest_tz, sched_arr_utc) as sched_arr_local
    from {{ ref('fct_flights_hist') }}
)
select
    carrier,
    flight_number,
    origin,
    dest,
    flight_date,
    weekday,
    hour(sched_dep_local) * 60 + minute(sched_dep_local) as dep_minute_local,
    hour(sched_arr_local) * 60 + minute(sched_arr_local) as arr_minute_local,
    -- Destination date minus origin date: 1 for a red-eye, -1 across the date line (GUM-HNL).
    date_diff('day', flight_date, sched_arr_local::date) as arr_day_offset
from local_times
