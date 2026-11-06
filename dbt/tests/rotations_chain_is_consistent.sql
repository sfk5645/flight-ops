-- When the chain is trusted, the two legs must really connect: the previous leg landed at this
-- leg's origin, before this leg took off, with a positive planned turnaround.
select r.flight_id, p.dest as prev_dest, f.origin, r.sched_turn_min
from {{ ref('fct_rotations') }} as r
join {{ ref('fct_flights_hist') }} as f on f.flight_id = r.flight_id
join {{ ref('fct_flights_hist') }} as p on p.flight_id = r.prev_flight_id
where r.chain_ok
  and (p.dest <> f.origin or p.arr_utc > f.dep_utc or r.sched_turn_min <= 0)
