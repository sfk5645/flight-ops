{{ config(materialized='table') }}

-- Inbound-aircraft features for training_set, one row per completed flight (flight_key =
-- hash(carrier, flight_number, origin, dest), sched_dep_utc). Kept apart from training_set and
-- stored as a table in the local DuckDB file: computed inside the one big training_set query it
-- ran out of memory. Same prediction moment (prediction_lead_min) and point-in-time rules.
with flights as (
    select
        flight_id, flight_date, carrier, flight_number, origin, dest, status, tail_number,
        sched_dep_utc, arr_delay_min, air_time_min, taxi_in_min,
        year * 12 + month - 1 as month_index,
        sched_dep_utc - to_minutes({{ var('prediction_lead_min') }}) as prediction_time,
        dep_utc + to_minutes(taxi_out_min) as takeoff_utc,
        arr_utc - to_minutes(taxi_in_min) as landing_utc
    from {{ ref('fct_flights_hist') }}
),

completed as (
    select * from flights where status = 'completed' and arr_delay_min is not null
),

-- Inbound aircraft. Live, nothing free says which plane will fly a flight, so the model gets a
-- guess, made the same way in training: the flight that most often brought the plane in over
-- the last 7 days. Tracking learns these pairs by following each plane (it lands as one
-- callsign and takes off as the next), so a pair is known once the later leg has taken off.
-- Measured on Jul-Aug 2026: a guess for 97.5% of flights, right 54% of the time overall, and
-- 97% when the same inbound flew all 7 days, so the number of days goes in as a confidence.
-- Flights are matched by a 64-bit hash of their identifying columns: comparing 11M x 7 rows of
-- text ran out of memory.
legs as (
    select
        hash(carrier, flight_number, origin, dest) as flight_key,
        hash(carrier, flight_number, origin) as callsign_key,
        flight_id,
        flight_date,
        origin,
        dest,
        takeoff_utc,
        landing_utc
    from flights
    where status <> 'cancelled' and takeoff_utc is not null
),

-- Each leg with the moment its plane took off again (live: tracking follows the plane's
-- transponder from the landing callsign to its next takeoff).
legs_next as (
    select
        hash(carrier, flight_number, origin) as callsign_key,
        takeoff_utc,
        landing_utc,
        dest,
        case when tail_number is not null then
            lead(takeoff_utc) over (partition by tail_number order by takeoff_utc)
        end as next_takeoff_utc
    from flights
    where status <> 'cancelled' and takeoff_utc is not null
),

-- One row per flight, day and the flight that brought its plane in that day.
rotation_days as (
    select
        f.flight_key,
        f.flight_date,
        p.callsign_key as in_key,
        p.origin as in_origin,
        min(f.takeoff_utc) as first_takeoff,
        min(p.takeoff_utc) as in_takeoff
    from {{ ref('fct_rotations') }} as r
    join legs as f on f.flight_id = r.flight_id
    join legs as p on p.flight_id = r.prev_flight_id
    where r.chain_ok
    group by all
),

-- Every inbound seen in the last 7 days is a candidate. Today check (live: the same tracking
-- lookups): a candidate is ruled out if its leg today already landed somewhere else, or landed
-- here and the plane has since taken off again on another flight; it is overdue if it hasn't
-- been seen though its usual takeoff (average over the days it flew; a median ran out of memory,
-- and delayed days only make "overdue" more cautious) was 30+ minutes ago.
-- Pick: not ruled out, then not overdue, then most days, most recent, then by hash (reruns match).
candidates as (
    select t.flight_key, t.sched_dep_utc, t.prediction_time, t.origin, t.month_index,
        r.in_key, r.in_origin,
        count(*) as days,
        max(r.flight_date) as last_day,
        to_timestamp(avg(epoch(r.in_takeoff) + (t.flight_date - r.flight_date) * 86400))
            as usual_takeoff
    from (
        select hash(carrier, flight_number, origin, dest) as flight_key, flight_date,
            sched_dep_utc, prediction_time, origin, month_index
        from completed
    ) as t
    join rotation_days as r
        on r.flight_key = t.flight_key
        and r.flight_date between t.flight_date - 7 and t.flight_date - 1
        and r.first_takeoff < t.prediction_time
    group by all
),

-- Each candidate's latest leg that took off before prediction time (an ASOF join finds it
-- directly; joining on callsign alone pulled every leg it flew in 2 years and ran out of
-- memory). Only a leg from the last 18 hours counts as today's.
candidate_today as (
    select
        c.flight_key, c.sched_dep_utc, c.prediction_time, c.origin, c.month_index, c.in_key,
        c.in_origin, c.days,
        c.last_day, c.usual_takeoff,
        i.takeoff_utc > c.prediction_time - interval 18 hour as seen,
        case when seen then i.takeoff_utc end as takeoff_utc,
        case when seen then i.landing_utc end as landing_utc,
        case when seen then i.dest end as landed_at,
        case when seen then i.next_takeoff_utc end as next_takeoff_utc
    from candidates as c
    asof left join legs_next as i
        on i.callsign_key = c.in_key and i.takeoff_utc <= c.prediction_time
),

-- Materialized: three steps below read it, and recomputing the pick each time ran out of memory.
inbound_guess as materialized (
    select
        flight_key,
        sched_dep_utc,
        any_value(prediction_time) as prediction_time,
        any_value(origin) as origin,
        any_value(month_index) as month_index,
        arg_max(
            struct_pack(days, in_origin, takeoff_utc, landing_utc),
            struct_pack(
                usable := not coalesce(
                    landing_utc <= prediction_time
                    and (landed_at <> origin or next_takeoff_utc <= prediction_time),
                    false
                ),
                on_time := not (
                    takeoff_utc is null and usual_takeoff < prediction_time - interval 30 minute
                ),
                days, last_day, in_key
            )
        ) as top
    from candidate_today
    group by flight_key, sched_dep_utc
),

-- Typical air time per route and taxi-in per airport, from the published months (as above).
air_time_monthly as (
    select origin, dest, month_index, count(air_time_min) as n, sum(air_time_min) as total
    from completed
    group by all
),

typical_air_time as (
    select t.in_origin, t.origin, t.month_index, sum(s.total) / sum(s.n) as air_min
    from (select distinct top.in_origin, origin, month_index from inbound_guess) as t
    join air_time_monthly as s
        on s.origin = t.in_origin and s.dest = t.origin
        and s.month_index between t.month_index - {{ var('bts_lag_months') }} - 2
            and t.month_index - {{ var('bts_lag_months') }}
    group by all
),

taxi_in_monthly as (
    select dest as airport, month_index, count(taxi_in_min) as n, sum(taxi_in_min) as total
    from completed
    group by all
),

typical_taxi_in as (
    select t.origin, t.month_index, sum(s.total) / sum(s.n) as taxi_in_min
    from (select distinct origin, month_index from inbound_guess) as t
    join taxi_in_monthly as s
        on s.airport = t.origin
        and s.month_index between t.month_index - {{ var('bts_lag_months') }} - 2
            and t.month_index - {{ var('bts_lag_months') }}
    group by all
),

-- Where the chosen inbound is at prediction time: its latest leg that took off in the 18 hours
-- before (looked up in candidate_today; live: the tracked flight with that callsign).
inbound_today as (
    select
        flight_key, sched_dep_utc, prediction_time, origin, month_index,
        top.days as days_same,
        top.in_origin as in_origin,
        top.takeoff_utc as takeoff_utc,
        top.landing_utc as landing_utc
    from inbound_guess
),

-- Turn buffer: scheduled departure minus when the plane can be at the gate (expected landing
-- plus typical taxi-in). Expected landing, using only what is visible at prediction time:
--   landed      its landing time
--   airborne    takeoff + typical air time (or now, if it's already running long)
--   not_seen    now + typical air time: the earliest it could land if it took off right now
inbound as (
    select
        t.flight_key,
        t.sched_dep_utc,
        t.days_same,
        case
            when t.takeoff_utc is null then 'not_seen'
            when t.landing_utc <= t.prediction_time then 'landed'
            else 'airborne'
        end as status,
        case
            when t.takeoff_utc is null then epoch(t.prediction_time) + a.air_min * 60
            when t.landing_utc <= t.prediction_time then epoch(t.landing_utc)
            else greatest(epoch(t.prediction_time), epoch(t.takeoff_utc) + a.air_min * 60)
        end as expected_landing_s,
        round((epoch(t.sched_dep_utc) - expected_landing_s) / 60 - x.taxi_in_min)::integer
            as buffer_min
    from inbound_today as t
    left join typical_air_time as a using (in_origin, origin, month_index)
    left join typical_taxi_in as x using (origin, month_index)
)

select flight_key, sched_dep_utc, days_same, status, buffer_min
from inbound
