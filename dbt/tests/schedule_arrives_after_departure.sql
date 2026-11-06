-- Scheduled arrival is derived from scheduled departure + scheduled duration, so it must be later.
select flight_id, sched_dep_utc, sched_arr_utc
from {{ ref('fct_flights_hist') }}
where sched_arr_utc <= sched_dep_utc
