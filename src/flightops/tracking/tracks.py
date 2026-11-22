"""Moments along each tracked flight: the training examples for the in-flight model.

The in-flight model answers "this plane is here, this high, this fast: how long until it
lands?". To learn that it needs many past moments with the answer known: where a plane was
during its flight, and when it then landed. Raw snapshots are deleted after 3 days, so the
daily job keeps a thinned copy of each flight's positions here (the landing time, airports and
flight number stay in silver/tracked_flights; join on icao24 + first_air_time).

Thinning: the first position in each EVERY_MIN-minute stretch since the flight's first
airborne sighting. Nov 21 2026: 92,714 flights with a known landing; every 5 minutes (all
positions) 1.5M rows / 26 MB a day, every 10 min 15 MB, every 15 min 0.59M rows / 11 MB
(~1 GB over the 90-day window). Moments fall at different minutes-before-landing on
different flights, so 15 minutes still covers the whole approach.

Only flights whose destination is known (seen or inferred, not ambiguous) are kept: without
a landing there is no answer to learn from.
"""

import pyarrow as pa

from flightops.duck import connect
from flightops.tracking.segments import clean_positions

EVERY_MIN = 15
USABLE_DEST = ("seen", "inferred")

_POINTS = """
select
    f.icao24,
    f.first_air_time,
    s.t as position_time,
    s.latitude::float as latitude,
    s.longitude::float as longitude,
    round(s.altitude_m)::float as altitude_m,
    round(s.speed_ms, 1)::float as speed_ms,
    round(s.vertical_rate_ms, 1)::float as vertical_rate_ms,
    round(s.track_deg)::float as track_deg
from flights f
join smoothed s
    on s.icao24 = f.icao24
    and s.airborne
    and s.t between f.first_air_time and f.last_air_time
where f.dest_how in {usable} and f.landing_time is not null
qualify row_number() over (
    partition by f.icao24, f.first_air_time, floor(epoch(s.t - f.first_air_time) / {every_s})
    order by s.t
) = 1
order by f.icao24, f.first_air_time, s.t
"""


def track_points(positions: pa.Table, flights: pa.Table, every_min: int = EVERY_MIN) -> pa.Table:
    """Thinned airborne positions of `flights` (tracked_flights rows) from the same snapshots."""
    con = connect()
    clean_positions(con, positions)
    con.register("flights", flights)
    return con.sql(_POINTS.format(usable=USABLE_DEST, every_s=every_min * 60)).to_arrow_table()
