"""Match the ends of each flight leg to airports and estimate takeoff and landing times.

Seen (origin_how / dest_how = 'seen'): the plane's last ground position before takeoff, or
first after landing, is on the airport itself, so the nearest airport within SEEN_RADIUS_M is
used. On Nov 9 2026 the median seen landing was 0.9 km from the airport's reference point and
90% were within 2 km; large airports' runway ends can be ~5 km away.

Inferred ('inferred'): most landings are never seen on the ground, because receivers lose
planes low on approach. A leg that vanished low and descending is projected forward to its
touchdown point (time to touchdown = height above the airport / descent rate, distance =
ground speed x that time, along its heading), and the airport nearest that point is used.
Height is measured above each candidate airport's elevation, so Denver (1,650 m) works like
LaGuardia (6 m). Takeoffs work the same way backwards, for legs first seen low and climbing.

Ambiguous ('inferred_ambiguous'): when a second airport is almost as close to the projected
point as the chosen one (big airports with small neighbours, like LAX and Hawthorne), the
pick is unreliable and is marked so that labels and training can skip it.

Previous leg ('previous_leg'): a plane takes off from where it last landed. When a takeoff
wasn't seen, the same plane's previous destination (seen or inferred, not ambiguous) is used
instead of the projection. On Nov 9 2026, seen takeoffs matched the previous leg's landing
airport in 31,783 of 31,796 cases. Destinations are not filled from the next leg: a plane lost
at cruise may have flown somewhere else first.

Times: the touchdown estimate is used for both kinds. For seen ends it is kept inside the
window between the last airborne row and the first ground row (and likewise for takeoffs).
"""

import pyarrow as pa

from flightops.duck import connect
from flightops.tracking.geo import distance_m

SEEN_RADIUS_M = 6000

# Inferred ends: the plane must have been at most this high above the airport when lost,
# and either descending (climbing, for takeoffs) or already very low.
INFERRED_MAX_HEIGHT_M = 1000
INFERRED_VERY_LOW_M = 300
INFERRED_MIN_RATE_MS = 1.0
# The projected touchdown (or liftoff) point must be this close to the airport.
INFERRED_RADIUS_M = 8000
# ...and the next-nearest airport must be at least this much further away, or the pick is
# marked ambiguous. Checked on Nov 9 2026 against the plane's previous seen landing: inferred
# takeoffs were right 99.1% of the time when not ambiguous and 75% when ambiguous (8% of them);
# with 3 km, 98.3% and 69%. Inferred landings were 99.8% right either way.
AMBIGUITY_MARGIN_M = 5000

# Floor and cap for the time-to-touchdown estimate. 2.5 m/s is ~500 ft/min, a gentle descent;
# a level plane would otherwise get an endless estimate.
MIN_RATE_MS = 2.5
MAX_ESTIMATE_S = 600

# Airliners don't use these, and a heliport inside a big airport would otherwise win.
EXCLUDED_TYPES = ("heliport", "seaplane_base")

_METRES_PER_DEGREE = 111_195
_FEET_TO_METRES = 0.3048


def _box(lat: str, lon: str, radius_m: float) -> str:
    """Cheap lat/lon box around a point, so exact distances only run on a few airports."""
    deg = radius_m / _METRES_PER_DEGREE
    return f"""a.latitude between {lat} - {deg} and {lat} + {deg}
        and a.longitude between {lon} - {deg} / greatest(cos(radians({lat})), 0.1)
            and {lon} + {deg} / greatest(cos(radians({lat})), 0.1)"""


_SEEN = """
create temp table {name} as
select l.icao24, l.first_air_time, a.iata, a.elevation_m
from legs l
join usable_airports a on {box}
where {distance} <= {radius}
qualify row_number() over (
    partition by l.icao24, l.first_air_time order by {distance}, a.iata
) = 1
"""

# {end} is "last" for landings and "first" for takeoffs. {sign} turns a climb rate into a
# positive number for takeoffs and a descent rate into one for landings; {heading} points
# forward from the last position for landings and backwards from the first for takeoffs.
_INFERRED = """
create temp table {name} as
with ends as (
    select
        icao24, first_air_time,
        {end}_latitude as lat, {end}_longitude as lon, {end}_altitude_m as altitude_m,
        {end}_speed_ms as speed_ms, {heading} as heading_deg,
        {sign} * coalesce({end}_vertical_rate_ms, 0) as rate_ms
    from legs
    where {reason}
        and {end}_altitude_m is not null
        and {end}_speed_ms is not null
        and {end}_track_deg is not null
),
candidates as (
    select e.*, a.iata, a.latitude as airport_lat, a.longitude as airport_lon,
        e.altitude_m - a.elevation_m as height_m
    from ends e
    join usable_airports a on {box}
),
projected as (
    select
        *,
        least(greatest(height_m, 0) / greatest(rate_ms, {min_rate}), {max_s}) as seconds
    from candidates
    where height_m <= {max_height}
        and (rate_ms >= {min_climb_or_descent} or height_m <= {very_low})
),
point as (
    select
        *,
        lat + speed_ms * seconds * cos(radians(heading_deg)) / {m_per_deg} as touch_lat,
        lon + speed_ms * seconds * sin(radians(heading_deg))
            / ({m_per_deg} * greatest(cos(radians(lat)), 0.1)) as touch_lon
    from projected
),
ranked as (
    select
        icao24, first_air_time, iata, seconds,
        {distance} as distance_m,
        row_number() over w as rank,
        nth_value({distance}, 2) over (w rows between unbounded preceding
            and unbounded following) as runner_up_m
    from point
    window w as (partition by icao24, first_air_time order by {distance}, iata)
)
select
    icao24, first_air_time, iata, seconds,
    coalesce(runner_up_m - distance_m < {margin}, false) as ambiguous
from ranked
where rank = 1 and distance_m <= {radius}
"""

_MATCHED = f"""
create temp table matched as
select
    l.*,
    coalesce(so.iata, io.iata) as origin,
    case
        when so.iata is not null then 'seen'
        when io.ambiguous then 'inferred_ambiguous'
        when io.iata is not null then 'inferred'
    end as origin_how,
    coalesce(sd.iata, id.iata) as dest,
    case
        when sd.iata is not null then 'seen'
        when id.ambiguous then 'inferred_ambiguous'
        when id.iata is not null then 'inferred'
    end as dest_how,
    case
        when so.iata is not null then l.first_air_time - to_seconds(least(
            coalesce(
                greatest(l.first_altitude_m - so.elevation_m, 0)
                    / greatest(coalesce(l.first_vertical_rate_ms, 0), {MIN_RATE_MS}),
                epoch(l.first_air_time - l.last_ground_time) / 2
            ),
            epoch(l.first_air_time - l.last_ground_time)
        ))
        when io.iata is not null then l.first_air_time - to_seconds(io.seconds)
    end as takeoff_time,
    case
        when sd.iata is not null then l.last_air_time + to_seconds(least(
            coalesce(
                greatest(l.last_altitude_m - sd.elevation_m, 0)
                    / greatest(-coalesce(l.last_vertical_rate_ms, 0), {MIN_RATE_MS}),
                epoch(l.first_ground_time - l.last_air_time) / 2
            ),
            epoch(l.first_ground_time - l.last_air_time)
        ))
        when id.iata is not null then l.last_air_time + to_seconds(id.seconds)
    end as landing_time
from legs l
left join seen_origins so using (icao24, first_air_time)
left join inferred_origins io using (icao24, first_air_time)
left join seen_dests sd using (icao24, first_air_time)
left join inferred_dests id using (icao24, first_air_time)
"""

_WITH_PREVIOUS_LEG = """
with previous as (
    select
        *,
        lag(dest) over w as prev_dest,
        lag(dest_how) over w in ('seen', 'inferred') as prev_dest_usable
    from matched
    window w as (partition by icao24 order by first_air_time)
)
select * exclude (prev_dest, prev_dest_usable, use_previous) replace (
    case when use_previous then prev_dest else origin end as origin,
    case when use_previous then 'previous_leg' else origin_how end as origin_how
)
from (
    select *, origin_how is distinct from 'seen' and coalesce(prev_dest_usable, false)
        as use_previous
    from previous
)
order by icao24, first_air_time
"""


def _seen(name: str, end: str) -> str:
    lat, lon = f"l.{end}_ground_latitude", f"l.{end}_ground_longitude"
    return _SEEN.format(
        name=name,
        box=_box(lat, lon, SEEN_RADIUS_M),
        distance=distance_m(lat, lon, "a.latitude", "a.longitude"),
        radius=SEEN_RADIUS_M,
    )


def _inferred(name: str, end: str, reason: str, sign: int, heading: str) -> str:
    # The touchdown point can be up to speed x MAX_ESTIMATE_S away, plus the match radius.
    reach_m = 120 * MAX_ESTIMATE_S + INFERRED_RADIUS_M
    return _INFERRED.format(
        name=name,
        end=end,
        reason=reason,
        sign=sign,
        heading=heading,
        box=_box("e.lat", "e.lon", reach_m),
        min_rate=MIN_RATE_MS,
        max_s=MAX_ESTIMATE_S,
        max_height=INFERRED_MAX_HEIGHT_M,
        min_climb_or_descent=INFERRED_MIN_RATE_MS,
        very_low=INFERRED_VERY_LOW_M,
        m_per_deg=_METRES_PER_DEGREE,
        distance=distance_m("touch_lat", "touch_lon", "airport_lat", "airport_lon"),
        radius=INFERRED_RADIUS_M,
        margin=AMBIGUITY_MARGIN_M,
    )


def match_airports(legs: pa.Table, airports: pa.Table) -> pa.Table:
    """Add origin / dest (IATA), how each was found, and takeoff / landing time estimates."""
    con = connect()
    con.register("legs", legs)
    con.register("airports", airports)
    excluded = ", ".join(f"'{t}'" for t in EXCLUDED_TYPES)
    con.execute(f"""
        create temp table usable_airports as
        select iata, latitude, longitude, coalesce(elevation_ft, 0) * {_FEET_TO_METRES}
            as elevation_m
        from airports
        where type not in ({excluded})
    """)
    con.execute(_seen("seen_origins", "last"))
    con.execute(_seen("seen_dests", "first"))
    con.execute(
        _inferred(
            "inferred_origins",
            end="first",
            reason="start_reason = 'appeared_airborne'",
            sign=1,
            heading="first_track_deg + 180",
        )
    )
    con.execute(
        _inferred(
            "inferred_dests",
            end="last",
            reason="end_reason = 'vanished'",
            sign=-1,
            heading="last_track_deg",
        )
    )
    con.execute(_MATCHED)
    return con.sql(_WITH_PREVIOUS_LEG).to_arrow_table()
