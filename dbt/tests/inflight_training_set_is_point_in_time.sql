-- Point-in-time check: recompute inputs for a sample of in-flight training rows straight from
-- the tracked flights and BTS, and require an exact match:
--   expected destination   only flights of the same feed, callsign and origin that took off in
--                          the 7 days before this one and landed before it took off; the most
--                          frequent destination (ties: the most recent) must be the one stored
--   route usual air time   only BTS flights dated in the 3 published months (M-5 .. M-3 for a
--                          moment in month M)
--   time flown             counted up to the moment, never past it
--   destination landings   recounted from the flights: same feed, landed at the destination
--                          in the hour ending at the last 15-minute mark >= 20 minutes back
-- A feature that used the flight's own landing (where it actually went) or newer flights
-- would differ.
with sample as (
    select * from {{ ref('inflight_training_set') }}
    using sample 300 rows (reservoir, 7)
),

flight as (
    select s.*, f.callsign as f_callsign
    from sample as s
    join {{ ref('stg_tracked_flights') }} as f using (feed, icao24, first_air_time)
),

history as (
    select
        s.feed, s.icao24, s.first_air_time, h.dest,
        count(*) as n,
        max(h.first_air_time) as last
    from flight as s
    join {{ ref('stg_tracked_flights') }} as h
        on h.feed = s.feed
        and h.callsign = s.f_callsign
        and h.origin = s.origin
        and h.dest_how in ('seen', 'inferred')
        and h.landing_time < s.first_air_time
        and h.first_air_time >= s.first_air_time - interval 7 day
    group by all
),

recomputed_dest as (
    select feed, icao24, first_air_time, arg_max(dest, struct_pack(n := n, last := last)) as dest
    from history
    group by all
),

recomputed_air as (
    select s.feed, s.icao24, s.position_time, avg(b.air_time_min) as air_min
    from sample as s
    join {{ ref('fct_flights_hist') }} as b
        on b.origin = s.origin
        and b.dest = s.dest
        and b.air_time_min is not null
        and b.flight_date >= (
            date_trunc('month', s.position_time) - to_months({{ var('bts_lag_months') }} + 2)
        )::date
        and b.flight_date < (
            date_trunc('month', s.position_time) - to_months({{ var('bts_lag_months') }} - 1)
        )::date
    group by all
),

recounted_landings as (
    select s.feed, s.icao24, s.position_time, count(f.icao24) as landings
    from sample as s
    left join {{ ref('stg_tracked_flights') }} as f
        on f.feed = s.feed
        and f.dest = s.dest
        and f.dest_how in ('seen', 'inferred')
        and f.landing_time >= time_bucket(interval 15 minute, s.position_time - interval 35 minute)
            - interval 45 minute
        and f.landing_time < time_bucket(interval 15 minute, s.position_time - interval 35 minute)
            + interval 15 minute
    group by all
)

select s.feed, s.icao24, s.position_time, s.dest, d.dest as dest_check,
    s.dest_landings_1h, n.landings as landings_check,
    s.route_air_min, a.air_min as route_air_check, s.minutes_flown
from sample as s
left join recomputed_dest as d using (feed, icao24, first_air_time)
left join recomputed_air as a using (feed, icao24, position_time)
left join recounted_landings as n using (feed, icao24, position_time)
where s.dest is distinct from d.dest
    or s.dest_landings_1h is distinct from n.landings
    or abs(coalesce(s.route_air_min, -1) - coalesce(a.air_min, -1)) > 0.001
    or s.minutes_flown < 0
    or (
        not s.takeoff_known
        and abs(s.minutes_flown - epoch(s.position_time - s.first_air_time) / 60) > 0.001
    )
