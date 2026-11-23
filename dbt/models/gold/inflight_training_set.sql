{{
    config(
        location=var('lake') ~ '/gold/inflight_training_set',
        options={'partition_by': 'date', 'overwrite_or_ignore': true},
    )
}}

-- In-flight model training rows: one per track point (a moment during a flight, every 15
-- minutes) of a flight with a known landing, heading for a BTS airport. Label:
-- minutes_to_landing. `date` is the UTC day the flight took off (all of a flight's moments
-- share it, so a time split never puts one flight on both sides).
--
-- Every input must be known at the moment itself, live, from tracking:
--   the plane now         position, altitude, speed, climb rate, heading (the track point)
--   the flight so far     origin and takeoff time (known once it is airborne)
--   destination           NOT where it actually landed (known only afterwards) but where the
--                         same callsign from the same origin usually went over the previous 7
--                         days of the same feed, counting flights that landed before this one
--                         took off. Measured Nov 2026 on adsb.lol: US airline flights have a
--                         guess 89.5% of the time and it is right 98%. dest_guess_right is kept
--                         for analysis only, never as an input.
--   usual air time        mean BTS air time (wheels off to on) for the route over the 3 BTS
--                         months published by then (bts_lag_months, as in training_set)
-- tests/inflight_training_set_is_point_in_time.sql recomputes a sample from scratch.
with flights as (
    select * from {{ ref('stg_tracked_flights') }}
    where callsign is not null and origin is not null
),

-- Landed flights: the history guesses are made from.
landed as (
    select feed, callsign, origin, dest, first_air_time, landing_time
    from flights
    where dest_how in ('seen', 'inferred') and landing_time is not null
),

targets as (
    select *
    from flights
    where dest_how in ('seen', 'inferred') and landing_time is not null
),

dest_guess as (
    select
        feed,
        icao24,
        first_air_time,
        arg_max(dest, struct_pack(n := n, last := last)) as expected_dest,
        max(n) as expected_dest_flights
    from (
        select
            t.feed, t.icao24, t.first_air_time, h.dest,
            count(*) as n,
            max(h.first_air_time) as last
        from targets as t
        join landed as h
            on h.feed = t.feed
            and h.callsign = t.callsign
            and h.origin = t.origin
            and h.landing_time < t.first_air_time
            and h.first_air_time >= t.first_air_time - interval 7 day
        group by all
    )
    group by all
),

air_time_monthly as (
    select origin, dest, year * 12 + month - 1 as month_index,
        count(air_time_min) as n, sum(air_time_min) as total
    from {{ ref('fct_flights_hist') }}
    where air_time_min is not null
    group by all
),

bts_airports as (
    select distinct dest as iata from air_time_monthly
),

points as (
    select
        p.*,
        f.callsign,
        f.carrier,
        f.origin,
        f.origin_how,
        f.dest as actual_dest,
        f.takeoff_time,
        f.landing_time,
        g.expected_dest as dest,
        g.expected_dest_flights,
        a.latitude as dest_lat,
        a.longitude as dest_lon,
        a.tz as dest_tz,
        year(p.position_time) * 12 + month(p.position_time) - 1 as month_index
    from {{ ref('stg_track_points') }} as p
    join targets as f using (feed, icao24, first_air_time)
    join dest_guess as g using (feed, icao24, first_air_time)
    join bts_airports as b on b.iata = g.expected_dest
    join {{ ref('stg_airports') }} as a on a.iata = g.expected_dest
),

usual_air_time as (
    select t.origin, t.dest, t.month_index, sum(s.total) / sum(s.n) as air_min, sum(s.n) as n
    from (select distinct origin, dest, month_index from points) as t
    join air_time_monthly as s
        on s.origin = t.origin and s.dest = t.dest
        and s.month_index between t.month_index - {{ var('bts_lag_months') }} - 2
            and t.month_index - {{ var('bts_lag_months') }}
    group by all
),

features as (
    select
        p.feed,
        p.icao24,
        p.first_air_time,
        p.position_time,
        p.first_air_time::date as date,
        epoch(p.landing_time - p.position_time) / 60 as minutes_to_landing,
        p.callsign,
        p.carrier,
        p.origin,
        p.dest,
        p.actual_dest,
        p.dest = p.actual_dest as dest_guess_right,
        p.expected_dest_flights,
        p.altitude_m,
        p.speed_ms,
        p.vertical_rate_ms,
        -- great-circle distance and initial bearing from the plane to the destination
        2 * 6371 * asin(sqrt(
            pow(sin(radians(p.dest_lat - p.latitude) / 2), 2)
            + cos(radians(p.latitude)) * cos(radians(p.dest_lat))
            * pow(sin(radians(p.dest_lon - p.longitude) / 2), 2)
        )) as dest_distance_km,
        degrees(atan2(
            sin(radians(p.dest_lon - p.longitude)) * cos(radians(p.dest_lat)),
            cos(radians(p.latitude)) * sin(radians(p.dest_lat))
            - sin(radians(p.latitude)) * cos(radians(p.dest_lat))
            * cos(radians(p.dest_lon - p.longitude))
        )) as dest_bearing_deg,
        p.track_deg,
        -- takeoff time is estimated from the first sightings; a plane first seen at cruise
        -- has none, so time flown counts from the first sighting (takeoff_seen says which)
        p.takeoff_time is not null as takeoff_known,
        epoch(p.position_time - coalesce(p.takeoff_time, p.first_air_time)) / 60
            as minutes_flown,
        u.air_min as route_air_min,
        u.n as route_flights_3m,
        hour(timezone(p.dest_tz, p.position_time)) as dest_hour_local,
        dayofweek(timezone(p.dest_tz, p.position_time)) as dest_weekday_local
    from points as p
    left join usual_air_time as u using (origin, dest, month_index)
)

select
    * exclude (dest_bearing_deg, track_deg),
    -- 0 = flying straight at the destination, 180 = flying away from it
    abs(((track_deg - dest_bearing_deg + 540) % 360) - 180)::float as heading_off_deg,
    (route_air_min - minutes_flown)::float as route_remaining_min,
    -- simplest physics guess: distance left / current ground speed
    (dest_distance_km / nullif(speed_ms * 0.06, 0))::float as direct_estimate_min
from features
order by date, feed, icao24, first_air_time, position_time
