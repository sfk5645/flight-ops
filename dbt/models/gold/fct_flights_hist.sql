{{ config(materialized='view') }}

-- Every historical US domestic flight, one row each, with local-time fields the delay models
-- need (scheduled hour and weekday at the origin). Cancelled and diverted flights stay in with
-- their status; the training set filters to completed flights.
--
-- A view, not a stored table: it is silver plus a few columns that are cheap to compute, so
-- storing a second copy in R2 would only cost space. DuckDB computes it whenever it's read.
select
    f.flight_id,
    f.flight_date,
    f.carrier,
    f.flight_number,
    f.tail_number,
    f.origin,
    f.dest,
    f.origin || '-' || f.dest as route,
    f.status,
    f.sched_dep_utc,
    f.sched_arr_utc,
    f.dep_utc,
    f.arr_utc,
    timezone(o.tz, f.sched_dep_utc) as sched_dep_local,
    hour(timezone(o.tz, f.sched_dep_utc)) as sched_dep_hour_local,
    isodow(timezone(o.tz, f.sched_dep_utc)) as sched_dep_weekday_local,
    f.dep_delay_min,
    f.arr_delay_min,
    f.taxi_out_min,
    f.taxi_in_min,
    f.sched_elapsed_min,
    f.actual_elapsed_min,
    f.air_time_min,
    f.distance_mi,
    f.cancellation_code,
    f.carrier_delay_min,
    f.weather_delay_min,
    f.nas_delay_min,
    f.security_delay_min,
    f.late_aircraft_delay_min,
    o.tz as origin_tz,
    d.tz as dest_tz,
    f.year,
    f.month
from {{ ref('stg_bts_flights') }} as f
left join {{ ref('stg_airports') }} as o on o.iata = f.origin
left join {{ ref('stg_airports') }} as d on d.iata = f.dest
