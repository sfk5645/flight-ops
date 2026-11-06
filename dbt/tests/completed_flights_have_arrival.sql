-- A completed flight must have actual departure and arrival times, in that order.
select flight_id, dep_utc, arr_utc
from {{ ref('fct_flights_hist') }}
where status = 'completed'
  and (dep_utc is null or arr_utc is null or arr_utc <= dep_utc)
