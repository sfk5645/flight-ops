"""Split OpenSky position snapshots into flight legs (one row per flight).

Each aircraft (icao24) is sorted by time and every row is marked airborne or on the ground.
A leg is an unbroken run of airborne rows. It starts at a takeoff (ground -> air) or when the
plane appears already flying, and ends at a landing (air -> ground), when the plane vanishes
for more than GAP_S, or at the end of the data.

Cleaning before splitting:
- Stale rows: OpenSky keeps listing a plane for ~5 minutes after its last position, so one
  position can appear in several snapshots. Rows are timed by time_position (when the
  position was measured) and repeats of the same position are dropped.
- Blips: a single row whose on_ground disagrees with the rows just before and after it is
  flipped to match them. A real stop keeps an airliner on the ground for many snapshots, so
  a lone "on ground" row mid-flight is a glitch or a touch-and-go (counted in ground_blips).

Coverage holes: a plane flying over an ocean or a poorly covered region can be out of sight
for an hour or more and reappear at cruise height. Those pieces are joined back into one leg
when the plane disappeared and reappeared high up, with the same callsign, and the distance
between the two sightings fits the time gap at the plane's own speed (coverage_gaps counts
the joins). A landing and takeoff inside the gap would leave it short of that distance.

Airports, landing-time estimates and go-around merging come later, on top of these legs.
"""

import pyarrow as pa

from flightops.duck import connect
from flightops.tracking.geo import distance_m

# A gap longer than this ends a leg. Long enough that one missed 5-minute run (a 10-minute
# gap) doesn't split a flight, short enough that an unseen landing and turnaround does.
GAP_S = 20 * 60

# A lone row only counts as a blip when both neighbours are this close in time.
BLIP_WINDOW_S = 10 * 60

# Coverage holes are only joined when the plane was this high on both sides of the gap
# (well above the height where receivers lose planes on approach).
CRUISE_ALTITUDE_M = 3000

# Distance covered during the gap, as a share of what the plane's own speed would cover.
# Below the range it slowed or stopped (a landing); above it, the sightings don't fit.
GAP_SPEED_RANGE = (0.7, 1.3)

_OBSERVATIONS = """
create temp table observations as
select
    icao24,
    to_timestamp(time_position) as t,
    nullif(trim(callsign), '') as callsign,
    on_ground,
    coalesce(baro_altitude, geo_altitude) as altitude_m,
    velocity as speed_ms,
    vertical_rate as vertical_rate_ms,
    true_track as track_deg,
    latitude,
    longitude
from positions
where time_position is not null and latitude is not null and longitude is not null
qualify row_number() over (partition by icao24, time_position order by snapshot_time) = 1
"""

_SMOOTHED = f"""
create temp table smoothed as
with neighbours as (
    select
        *,
        coalesce(
            lag(on_ground) over w <> on_ground
            and lead(on_ground) over w <> on_ground
            and t - lag(t) over w <= interval {BLIP_WINDOW_S} second
            and lead(t) over w - t <= interval {BLIP_WINDOW_S} second,
            false
        ) as is_blip
    from observations
    window w as (partition by icao24 order by t)
)
select
    *,
    (not on_ground) <> is_blip as airborne,
    is_blip and on_ground as ground_blip
from neighbours
"""

_MARKED = f"""
create temp table marked as
with neighbours as (
    select
        *,
        lag(airborne) over w as prev_airborne,
        lag(t) over w as prev_t,
        lead(airborne) over w as next_airborne,
        lead(t) over w as next_t,
        lag(latitude) over w as prev_latitude,
        lag(longitude) over w as prev_longitude,
        lead(latitude) over w as next_latitude,
        lead(longitude) over w as next_longitude
    from smoothed
    window w as (partition by icao24 order by t)
),
edges as (
    select
        *,
        -- prev_t / next_t within the gap means the neighbouring row belongs to this stretch.
        prev_t is not null and t - prev_t <= interval {GAP_S} second as prev_close,
        next_t is not null and next_t - t <= interval {GAP_S} second as next_close
    from neighbours
)
select
    *,
    airborne and not (prev_close and prev_airborne) as starts_leg,
    airborne and not (next_close and next_airborne) as ends_leg,
    -- Running count of leg starts: every airborne row gets the number of the leg it is in.
    sum((airborne and not (prev_close and prev_airborne))::int)
        over (partition by icao24 order by t) as leg_no
from edges
"""

_CALLSIGNS = """
create temp table leg_callsigns as
select icao24, leg_no, callsign
from marked
where airborne and callsign is not null
group by icao24, leg_no, callsign
qualify row_number() over (
    partition by icao24, leg_no order by count(*) desc, callsign
) = 1
"""

_LEGS = f"""
create temp table pieces as
select
    m.icao24,
    c.callsign,
    min(m.t) as first_air_time,
    max(m.t) as last_air_time,
    max(case when m.starts_leg then
        case when m.prev_close then 'takeoff_seen' else 'appeared_airborne' end
    end) as start_reason,
    max(case when m.ends_leg then
        case
            when m.next_close then 'landing_seen'
            when m.next_t is null and m.t >= $data_end - interval {GAP_S} second
                then 'still_flying'
            else 'vanished'
        end
    end) as end_reason,
    -- The takeoff happened between last_ground_time and first_air_time, the landing between
    -- last_air_time and first_ground_time. The ground rows' positions are on the airport.
    max(case when m.starts_leg and m.prev_close then m.prev_t end) as last_ground_time,
    max(case when m.starts_leg and m.prev_close then m.prev_latitude end) as last_ground_latitude,
    max(case when m.starts_leg and m.prev_close then m.prev_longitude end)
        as last_ground_longitude,
    max(case when m.ends_leg and m.next_close then m.next_t end) as first_ground_time,
    max(case when m.ends_leg and m.next_close then m.next_latitude end) as first_ground_latitude,
    max(case when m.ends_leg and m.next_close then m.next_longitude end)
        as first_ground_longitude,
    count(*) as positions,
    sum(m.ground_blip::int) as ground_blips,
    max(m.altitude_m) as max_altitude_m,
    arg_min(m.latitude, m.t) as first_latitude,
    arg_min(m.longitude, m.t) as first_longitude,
    arg_min(m.altitude_m, m.t) as first_altitude_m,
    arg_min(m.vertical_rate_ms, m.t) as first_vertical_rate_ms,
    arg_min(m.speed_ms, m.t) as first_speed_ms,
    arg_min(m.track_deg, m.t) as first_track_deg,
    arg_max(m.latitude, m.t) as last_latitude,
    arg_max(m.longitude, m.t) as last_longitude,
    arg_max(m.altitude_m, m.t) as last_altitude_m,
    arg_max(m.vertical_rate_ms, m.t) as last_vertical_rate_ms,
    arg_max(m.speed_ms, m.t) as last_speed_ms,
    arg_max(m.track_deg, m.t) as last_track_deg
from marked m
left join leg_callsigns c using (icao24, leg_no)
where m.airborne
group by m.icao24, m.leg_no, c.callsign
"""

# Distance between where a piece was last seen and where the next one starts.
_GAP_DISTANCE_M = distance_m("prev_latitude", "prev_longitude", "first_latitude", "first_longitude")

_STITCHED = f"""
with neighbours as (
    select
        *,
        lag(end_reason) over w as prev_end_reason,
        lag(callsign) over w as prev_callsign,
        lag(last_air_time) over w as prev_last_air_time,
        lag(last_latitude) over w as prev_latitude,
        lag(last_longitude) over w as prev_longitude,
        lag(last_altitude_m) over w as prev_altitude_m,
        lag(last_speed_ms) over w as prev_speed_ms
    from pieces
    window w as (partition by icao24 order by first_air_time)
),
checked as (
    select
        *,
        coalesce(
            start_reason = 'appeared_airborne'
            and prev_end_reason = 'vanished'
            and callsign = prev_callsign
            and prev_altitude_m >= {CRUISE_ALTITUDE_M}
            and first_altitude_m >= {CRUISE_ALTITUDE_M}
            and {_GAP_DISTANCE_M}
                between {GAP_SPEED_RANGE[0]} * (prev_speed_ms + first_speed_ms) / 2
                    * epoch(first_air_time - prev_last_air_time)
                and {GAP_SPEED_RANGE[1]} * (prev_speed_ms + first_speed_ms) / 2
                    * epoch(first_air_time - prev_last_air_time),
            false
        ) as continues
    from neighbours
),
numbered as (
    -- Same running-sum trick as for legs: pieces that continue the previous one share its number.
    select *, sum((not continues)::int) over (partition by icao24 order by first_air_time) as n
    from checked
)
select
    icao24,
    arg_min(callsign, first_air_time) as callsign,
    min(first_air_time) as first_air_time,
    max(last_air_time) as last_air_time,
    arg_min(start_reason, first_air_time) as start_reason,
    arg_max(end_reason, last_air_time) as end_reason,
    arg_min(last_ground_time, first_air_time) as last_ground_time,
    arg_min(last_ground_latitude, first_air_time) as last_ground_latitude,
    arg_min(last_ground_longitude, first_air_time) as last_ground_longitude,
    arg_max(first_ground_time, last_air_time) as first_ground_time,
    arg_max(first_ground_latitude, last_air_time) as first_ground_latitude,
    arg_max(first_ground_longitude, last_air_time) as first_ground_longitude,
    sum(positions)::int as positions,
    sum(ground_blips)::int as ground_blips,
    (count(*) - 1)::int as coverage_gaps,
    max(max_altitude_m) as max_altitude_m,
    arg_min(first_latitude, first_air_time) as first_latitude,
    arg_min(first_longitude, first_air_time) as first_longitude,
    arg_min(first_altitude_m, first_air_time) as first_altitude_m,
    arg_min(first_vertical_rate_ms, first_air_time) as first_vertical_rate_ms,
    arg_min(first_speed_ms, first_air_time) as first_speed_ms,
    arg_min(first_track_deg, first_air_time) as first_track_deg,
    arg_max(last_latitude, last_air_time) as last_latitude,
    arg_max(last_longitude, last_air_time) as last_longitude,
    arg_max(last_altitude_m, last_air_time) as last_altitude_m,
    arg_max(last_vertical_rate_ms, last_air_time) as last_vertical_rate_ms,
    arg_max(last_speed_ms, last_air_time) as last_speed_ms,
    arg_max(last_track_deg, last_air_time) as last_track_deg
from numbered
group by icao24, n
order by icao24, first_air_time
"""


def split_legs(positions: pa.Table) -> pa.Table:
    """One row per flight leg found in a set of OpenSky snapshots (bronze schema).

    A leg is identified by (icao24, first_air_time). Output is sorted, so the same input
    gives identical output regardless of row order.
    """
    con = connect()
    con.register("positions", positions)
    data_end = con.sql("select to_timestamp(max(snapshot_time)) from positions").fetchall()[0][0]
    for statement in (_OBSERVATIONS, _SMOOTHED, _MARKED, _CALLSIGNS):
        con.execute(statement)
    con.execute(_LEGS, {"data_end": data_end})
    return con.sql(_STITCHED).to_arrow_table()
