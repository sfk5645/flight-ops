"""Grade one day of tracked flights against BTS, the official record of US airline flights.

BTS has no wheels-off / wheels-on columns in silver, but they follow exactly from what it has:
wheels_off = actual departure + taxi-out, wheels_on = actual arrival - taxi-in (whole minutes).

Two directions:
- found: of the BTS flights that took off during the graded window, how many did we track with
  the same carrier, flight number, origin and destination (takeoff within an hour)? A leg whose
  origin came from the previous landing may have no takeoff time; its first airborne sighting
  stands in.
- confirmed: of our flights for BTS carriers between BTS airports, how many are in BTS?

Only flights that took off GRADED_FROM to GRADED_UNTIL into the day and landed before
LAST_LANDING are graded: the positions cover one UTC day, so flights near midnight are cut off.

BTS dates flights by local departure date, so `bts` must hold the day before and after too.
"""

from datetime import UTC, date, datetime, time, timedelta

import pyarrow as pa

from flightops.duck import connect

GRADED_FROM = timedelta(hours=2)
GRADED_UNTIL = timedelta(hours=20)
LAST_LANDING = timedelta(hours=23, minutes=45)
MATCH_WINDOW_S = 3600

_SETUP = """
create temp table bts_times as
select
    *,
    dep_utc + to_minutes(taxi_out_min) as wheels_off,
    arr_utc - to_minutes(taxi_in_min) as wheels_on
from bts
where status in ('completed', 'diverted');

create temp table graded as
select * from bts_times
where wheels_off >= $graded_from and wheels_off < $graded_until and wheels_on < $last_landing
    and status = 'completed';

-- The closest tracked leg per BTS flight with the same carrier, number and route.
create temp table matched as
select g.*, f.takeoff_time, f.landing_time, f.origin_how, f.dest_how
from graded g
left join lateral (
    select *, coalesce(takeoff_time, first_air_time) as departed from flights f
    where f.carrier = g.carrier and f.flight_number = g.flight_number
        and f.origin = g.origin and f.dest = g.dest
        and abs(epoch(departed) - epoch(g.wheels_off)) < $window
    order by abs(epoch(departed) - epoch(g.wheels_off))
    limit 1
) f on true;
"""

_FOUND = """
select
    coalesce(carrier, 'all') as carrier,
    count(*) as bts_flights,
    count(origin_how) as found,
    round(100 * count(origin_how) / count(*), 1) as found_pct
from matched
group by rollup (carrier)
order by grouping(carrier) desc, bts_flights desc
"""

_CONFIRMED = """
with ours as (
    select * from flights
    where carrier in (select carrier from bts_times)
        and origin <> dest
        and origin in (select origin from bts_times) and dest in (select dest from bts_times)
        and coalesce(takeoff_time, first_air_time) >= $graded_from
        and coalesce(takeoff_time, first_air_time) < $graded_until
        and landing_time < $last_landing
)
select
    coalesce(origin_how || ' / ' || dest_how, 'all') as how,
    count(*) as tracked,
    round(100 * avg(exists (
        select 1 from bts_times b
        where b.carrier = o.carrier and b.flight_number = o.flight_number
            and b.origin = o.origin and b.dest = o.dest
            and abs(epoch(b.wheels_off) - epoch(coalesce(o.takeoff_time, o.first_air_time)))
                < $window
    )::int), 1) as in_bts_pct
from ours o
group by rollup (origin_how || ' / ' || dest_how)
order by grouping(origin_how || ' / ' || dest_how) desc, tracked desc
"""

_TIMING = """
with gaps as (
    select 'landing' as event, dest_how as how, epoch(landing_time - wheels_on) / 60 as gap_min
    from matched where landing_time is not null
    union all
    select 'takeoff', origin_how, epoch(takeoff_time - wheels_off) / 60
    from matched where takeoff_time is not null
)
select
    event,
    how,
    count(*) as flights,
    round(median(gap_min), 1) as median_min,
    round(median(abs(gap_min)), 1) as median_abs_min,
    round(100 * avg((abs(gap_min) <= 2)::int), 1) as within_2_min_pct,
    round(100 * avg((abs(gap_min) <= 5)::int), 1) as within_5_min_pct,
    round(quantile_cont(gap_min, 0.05), 1) as p05_min,
    round(quantile_cont(gap_min, 0.95), 1) as p95_min
from gaps
group by all
order by event, flights desc
"""

_MISSES = """
with legs as (
    select
        m.carrier, m.flight_number, m.origin, m.dest,
        f.origin as our_origin, f.dest as our_dest, f.first_air_time
    from matched m
    left join flights f
        on f.carrier = m.carrier and f.flight_number = m.flight_number
        and abs(epoch(f.first_air_time) - epoch(m.wheels_off)) < $window / 2
    where m.origin_how is null
),
missed as (
    select
        case
            when bool_and(first_air_time is null) then 'no leg with that flight number'
            when bool_or(our_origin is null and our_dest is null) then 'neither airport known'
            when bool_or(our_origin is null and our_dest = dest) then 'takeoff airport unknown'
            when bool_or(our_dest is null and our_origin = origin) then 'landing airport unknown'
            else 'different airport'
        end as reason
    from legs
    group by carrier, flight_number, origin, dest
)
select reason, count(*) as flights from missed group by reason order by flights desc
"""


def compare_with_bts(flights: pa.Table, bts: pa.Table, day: date) -> dict[str, pa.Table]:
    """Found / confirmed / timing / misses tables for `day`'s tracked flights."""
    start = datetime.combine(day, time(), tzinfo=UTC)
    params = {
        "graded_from": start + GRADED_FROM,
        "graded_until": start + GRADED_UNTIL,
        "last_landing": start + LAST_LANDING,
        "window": MATCH_WINDOW_S,
    }
    con = connect()
    con.register("flights", flights)
    con.register("bts", bts)
    for statement in filter(str.strip, _SETUP.split(";")):
        con.execute(statement, {k: v for k, v in params.items() if f"${k}" in statement})

    def run(sql: str) -> pa.Table:
        used = {k: v for k, v in params.items() if f"${k}" in sql}
        return con.sql(sql, params=used).to_arrow_table()

    return {
        "found": run(_FOUND),
        "confirmed": run(_CONFIRMED),
        "timing": run(_TIMING),
        "misses": run(_MISSES),
    }
