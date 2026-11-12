{{
    config(
        location=var('lake') ~ '/gold/training_set',
        options={'partition_by': 'year, month', 'overwrite_or_ignore': true},
    )
}}

-- Pre-departure model training rows: one per completed flight, with its inputs and the label
-- (arr_delay_min). Cancelled and diverted flights have no arrival delay; they stay in
-- fct_flights_hist with their status.
--
-- Every input must be known at prediction time (2 hours before scheduled departure):
--   the flight itself    from the schedule the user enters
--   delay history        only BTS months published by then (see macros/history.sql)
--   airport traffic      takeoffs / landings in the 3 whole hours before the prediction hour
--   inbound aircraft     a guess from the last 7 days of rotations, and where that flight is now
--   weather              the latest report at each airport, published by then
-- tests/training_set_history_is_point_in_time.sql recomputes a sample from scratch to prove it.
with flights as (
    select
        -- only the columns used below: the whole row would be read for every reference
        flight_id, flight_date, year, month, carrier, flight_number, origin, dest, dest_tz,
        status, sched_dep_utc, sched_arr_utc, dep_utc, arr_utc, sched_dep_hour_local,
        sched_dep_weekday_local, dep_delay_min, arr_delay_min, taxi_out_min, taxi_in_min,
        air_time_min, sched_elapsed_min, distance_mi,
        year * 12 + month - 1 as month_index,
        hour(timezone(dest_tz, sched_arr_utc)) as sched_arr_hour_local,
        sched_dep_utc - interval 2 hour as prediction_time,
        date_trunc('hour', sched_dep_utc - interval 2 hour) as prediction_hour,
        -- the moments our live tracking detects
        dep_utc + to_minutes(taxi_out_min) as takeoff_utc,
        arr_utc - to_minutes(taxi_in_min) as landing_utc
    from {{ ref('fct_flights_hist') }}
),

completed as (
    select * from flights where status = 'completed' and arr_delay_min is not null
),

route_history as ({{ delay_history('completed', 'completed', ['origin', 'dest'], 'arr_delay_min') }}),

flight_history as (
    {{ delay_history('completed', 'completed', ['carrier', 'flight_number', 'origin', 'dest'], 'arr_delay_min') }}
),

carrier_history as ({{ delay_history('completed', 'completed', ['carrier'], 'arr_delay_min') }}),

-- Departure delay at the origin and arrival delay at the destination, by local hour: how
-- congested each airport usually is at the time this flight uses it.
origin_hour_history as (
    {{ delay_history('completed', 'completed', ['origin', 'sched_dep_hour_local'], 'dep_delay_min') }}
),

dest_hour_history as (
    {{ delay_history('completed', 'completed', ['dest', 'sched_arr_hour_local'], 'arr_delay_min') }}
),

-- Signed days to the nearest federal holiday (positive: still ahead), per local date.
holidays as (
    select
        d.flight_date,
        arg_min(h.holiday_date - d.flight_date, abs(h.holiday_date - d.flight_date))
            as days_to_holiday
    from (select distinct flight_date from completed) as d
    cross join {{ ref('int_us_holidays') }} as h
    group by d.flight_date
),

-- Airport traffic: how many planes took off from (or landed at) each airport in the 3 whole
-- hours before the prediction hour, and how many is normal for that airport, local weekday and
-- hour. A slowdown (ratio well under 1) is the clearest sign of storms, ground stops or
-- congestion. Live, both the count and its normal come from tracked
-- flights of the same 12 BTS carriers, so the ratio is comparable even though tracking misses
-- about 10% of flights.
movements as (
    select origin as airport, date_trunc('hour', takeoff_utc) as hour_utc, 1 as deps, 0 as arrs
    from flights
    where status <> 'cancelled' and takeoff_utc is not null
    union all
    select dest, date_trunc('hour', landing_utc), 0, 1
    from flights
    where status = 'completed' and landing_utc is not null
),

-- Every airport and every hour, so hours with no traffic count as zero.
airport_hours as (
    select a.airport, a.tz, unnest(range(b.first_hour, b.last_hour, interval 1 hour)) as hour_utc
    from (
        select distinct m.airport, ap.tz
        from movements as m
        join {{ ref('stg_airports') }} as ap on ap.iata = m.airport
    ) as a
    cross join (
        select min(hour_utc) as first_hour, max(hour_utc) + interval 1 hour as last_hour
        from movements
    ) as b
),

traffic as (
    select
        h.airport,
        h.hour_utc,
        year(h.hour_utc) * 12 + month(h.hour_utc) - 1 as month_index,
        isodow(timezone(h.tz, h.hour_utc)) as weekday_local,
        hour(timezone(h.tz, h.hour_utc)) as hour_local,
        -- the 3 whole hours before this hour
        sum(coalesce(m.deps, 0)) over w as deps_3h,
        sum(coalesce(m.arrs, 0)) over w as arrs_3h
    from airport_hours as h
    left join (
        select airport, hour_utc, sum(deps) as deps, sum(arrs) as arrs
        from movements
        group by all
    ) as m using (airport, hour_utc)
    window w as (partition by h.airport order by h.hour_utc rows between 3 preceding and 1 preceding)
),

-- Normal traffic: the average 3-hour count for the same airport, local weekday and hour in the
-- latest published BTS months (as for delay history).
traffic_monthly as (
    select airport, weekday_local, hour_local, month_index,
        count(*) as hours, sum(deps_3h) as deps, sum(arrs_3h) as arrs
    from traffic
    group by all
),

traffic_normal as (
    select
        t.airport,
        t.weekday_local,
        t.hour_local,
        t.month_index,
        sum(n.deps) / sum(n.hours) as deps_3h_normal,
        sum(n.arrs) / sum(n.hours) as arrs_3h_normal
    from (select distinct airport, weekday_local, hour_local, month_index from traffic) as t
    join traffic_monthly as n
        on n.airport = t.airport and n.weekday_local = t.weekday_local
        and n.hour_local = t.hour_local
        and n.month_index between t.month_index - {{ var('bts_lag_months') }} - 2
            and t.month_index - {{ var('bts_lag_months') }}
    group by all
),

traffic_now as (
    select t.airport, t.hour_utc, t.deps_3h, t.arrs_3h, n.deps_3h_normal, n.arrs_3h_normal
    from traffic as t
    left join traffic_normal as n using (airport, weekday_local, hour_local, month_index)
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
        takeoff_utc,
        landing_utc
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
        min(f.takeoff_utc) as first_takeoff
    from {{ ref('fct_rotations') }} as r
    join legs as f on f.flight_id = r.flight_id
    join legs as p on p.flight_id = r.prev_flight_id
    where r.chain_ok
    group by all
),

inbound_guess as (
    select
        c.carrier,
        c.flight_number,
        c.origin,
        c.dest,
        c.sched_dep_utc,
        any_value(c.prediction_time) as prediction_time,
        any_value(c.month_index) as month_index,
        -- most days wins; ties go to the most recent, then the lower hash so reruns match
        max(struct_pack(d.days, d.last_day, d.in_key, d.in_origin)) as top
    from completed as c
    join (
        select t.flight_key, t.sched_dep_utc, r.in_key, r.in_origin,
            count(*) as days, max(r.flight_date) as last_day
        from (
            select hash(carrier, flight_number, origin, dest) as flight_key, flight_date,
                sched_dep_utc, prediction_time
            from completed
        ) as t
        join rotation_days as r
            on r.flight_key = t.flight_key
            and r.flight_date between t.flight_date - 7 and t.flight_date - 1
            and r.first_takeoff < t.prediction_time
        group by all
    ) as d
        on d.flight_key = hash(c.carrier, c.flight_number, c.origin, c.dest)
        and d.sched_dep_utc = c.sched_dep_utc
    group by c.carrier, c.flight_number, c.origin, c.dest, c.sched_dep_utc
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

-- Where the guessed inbound flight is at prediction time: its latest leg that took off in the
-- 18 hours before (live: the tracked flight with that callsign from that airport).
inbound_today as (
    select
        g.carrier,
        g.flight_number,
        g.origin,
        g.dest,
        g.sched_dep_utc,
        any_value(g.prediction_time) as prediction_time,
        any_value(g.month_index) as month_index,
        any_value(g.top.days) as days_same,
        any_value(g.top.in_origin) as in_origin,
        max(i.takeoff_utc) as takeoff_utc,
        arg_max(i.landing_utc, i.takeoff_utc) as landing_utc
    from inbound_guess as g
    left join legs as i
        on i.callsign_key = g.top.in_key
        and i.takeoff_utc > g.prediction_time - interval 18 hour
        and i.takeoff_utc <= g.prediction_time
    group by g.carrier, g.flight_number, g.origin, g.dest, g.sched_dep_utc
),

-- Turn buffer: scheduled departure minus when the plane can be at the gate (expected landing
-- plus typical taxi-in). Expected landing, using only what is visible at prediction time:
--   landed      its landing time
--   airborne    takeoff + typical air time (or now, if it's already running long)
--   not_seen    now + typical air time: the earliest it could land if it took off right now
inbound as (
    select
        t.carrier,
        t.flight_number,
        t.origin,
        t.dest,
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
,

-- Weather at both airports: the latest report observed at least 10 minutes before the
-- prediction time (reports take a few minutes to be published), and the one from 3 hours
-- before that, to see whether conditions are getting worse. Flight category as a number,
-- 0 = VFR (clear) .. 3 = LIFR (very low cloud or fog). A report more than 3 hours old means the
-- station went quiet; its values are left out rather than shown as current.
weather as (
    select
        airport,
        observed_utc,
        case flight_category
            when 'VFR' then 0 when 'MVFR' then 1 when 'IFR' then 2 when 'LIFR' then 3
        end::tinyint as category,
        thunderstorm,
        snow_or_ice,
        wind_kt,
        gust_kt
    from {{ ref('stg_airport_weather') }}
),

flight_times as (
    select distinct origin, dest, prediction_time,
        prediction_time - interval 10 minute as known_by,
        prediction_time - interval 10 minute - interval 3 hour as known_by_3h
    from completed
),

weather_now as (
    select
        f.origin,
        f.dest,
        f.prediction_time,
        {%- for end in ['origin', 'dest'] %}
        date_diff('minute', {{ end }}_now.observed_utc, f.prediction_time) as {{ end }}_wx_age_min,
        {{ end }}_now.observed_utc > f.known_by - interval 3 hour as {{ end }}_fresh,
        {{ end }}_now.category as {{ end }}_category,
        {{ end }}_now.thunderstorm as {{ end }}_thunderstorm,
        {{ end }}_now.snow_or_ice as {{ end }}_snow_or_ice,
        {{ end }}_now.wind_kt as {{ end }}_wind_kt,
        {{ end }}_now.gust_kt as {{ end }}_gust_kt,
        case when {{ end }}_before.observed_utc > f.known_by_3h - interval 3 hour
            then {{ end }}_before.category end as {{ end }}_category_3h_ago
        {%- if not loop.last %},{% endif %}
        {%- endfor %}
    from flight_times as f
    {%- for end in ['origin', 'dest'] %}
    asof left join weather as {{ end }}_now
        on {{ end }}_now.airport = f.{{ end }} and {{ end }}_now.observed_utc <= f.known_by
    asof left join weather as {{ end }}_before
        on {{ end }}_before.airport = f.{{ end }} and {{ end }}_before.observed_utc <= f.known_by_3h
    {%- endfor %}
)

-- Averages are rounded (minutes to 0.1, shares and ratios to 0.001) and stored as 4-byte
-- floats: full doubles don't compress and add nothing a model can use.
select
    -- identity: carrier, flight_number, origin, dest and sched_dep_utc identify the flight
    -- (flight_id is md5 of exactly these). flight_id itself isn't stored: random bytes don't
    -- compress, and it was half the table's size.
    c.flight_number,
    c.flight_date,
    c.sched_dep_utc,

    -- the flight itself
    c.carrier,
    c.origin,
    c.dest,
    c.distance_mi,
    c.sched_elapsed_min,
    c.sched_dep_hour_local,
    c.sched_arr_hour_local,
    c.sched_dep_weekday_local,
    month(c.flight_date) as month_of_year,
    hol.days_to_holiday,

    -- delay history (latest published BTS months)
    rh.n_3m as route_flights_3m,
    round(rh.avg_1m, 1)::float as route_delay_1m,
    round(rh.avg_3m, 1)::float as route_delay_3m,
    round(rh.late15_3m, 3)::float as route_late15_3m,
    fh.n_3m as flight_flights_3m,
    round(fh.avg_3m, 1)::float as flight_delay_3m,
    round(fh.late15_3m, 3)::float as flight_late15_3m,
    round(ch.avg_1m, 1)::float as carrier_delay_1m,
    round(ch.avg_3m, 1)::float as carrier_delay_3m,
    round(ch.late15_3m, 3)::float as carrier_late15_3m,
    round(oh.avg_3m, 1)::float as origin_hour_dep_delay_3m,
    round(dh.avg_3m, 1)::float as dest_hour_arr_delay_3m,

    -- airport traffic in the 3 hours before the prediction hour
    ot.deps_3h as origin_deps_3h,
    round(ot.deps_3h_normal, 1)::float as origin_deps_3h_normal,
    round(ot.deps_3h / nullif(ot.deps_3h_normal, 0), 3)::float as origin_deps_3h_ratio,
    dt.arrs_3h as dest_arrs_3h,
    round(dt.arrs_3h_normal, 1)::float as dest_arrs_3h_normal,
    round(dt.arrs_3h / nullif(dt.arrs_3h_normal, 0), 3)::float as dest_arrs_3h_ratio,

    -- inbound aircraft (guessed; null when the flight has no rotation in the last 7 days)
    ib.days_same as inbound_days_same,
    ib.status as inbound_status,
    ib.buffer_min as inbound_buffer_min,

    -- weather at both airports (latest report published by prediction time)
    wx.origin_wx_age_min,
    case when wx.origin_fresh then wx.origin_category end as origin_flight_category,
    case when wx.origin_fresh then wx.origin_category - wx.origin_category_3h_ago end
        as origin_category_change_3h,
    case when wx.origin_fresh then wx.origin_thunderstorm end as origin_thunderstorm,
    case when wx.origin_fresh then wx.origin_snow_or_ice end as origin_snow_or_ice,
    case when wx.origin_fresh then wx.origin_wind_kt end as origin_wind_kt,
    case when wx.origin_fresh then wx.origin_gust_kt end as origin_gust_kt,
    wx.dest_wx_age_min,
    case when wx.dest_fresh then wx.dest_category end as dest_flight_category,
    case when wx.dest_fresh then wx.dest_category - wx.dest_category_3h_ago end
        as dest_category_change_3h,
    case when wx.dest_fresh then wx.dest_thunderstorm end as dest_thunderstorm,
    case when wx.dest_fresh then wx.dest_snow_or_ice end as dest_snow_or_ice,
    case when wx.dest_fresh then wx.dest_wind_kt end as dest_wind_kt,
    case when wx.dest_fresh then wx.dest_gust_kt end as dest_gust_kt,

    -- label
    c.arr_delay_min,

    c.year,
    c.month
from completed as c
left join holidays as hol on hol.flight_date = c.flight_date
left join route_history as rh
    on rh.origin = c.origin and rh.dest = c.dest and rh.month_index = c.month_index
left join flight_history as fh
    on fh.carrier = c.carrier and fh.flight_number = c.flight_number
    and fh.origin = c.origin and fh.dest = c.dest and fh.month_index = c.month_index
left join carrier_history as ch on ch.carrier = c.carrier and ch.month_index = c.month_index
left join origin_hour_history as oh
    on oh.origin = c.origin and oh.sched_dep_hour_local = c.sched_dep_hour_local
    and oh.month_index = c.month_index
left join dest_hour_history as dh
    on dh.dest = c.dest and dh.sched_arr_hour_local = c.sched_arr_hour_local
    and dh.month_index = c.month_index
left join traffic_now as ot on ot.airport = c.origin and ot.hour_utc = c.prediction_hour
left join traffic_now as dt on dt.airport = c.dest and dt.hour_utc = c.prediction_hour
left join inbound as ib
    on ib.carrier = c.carrier and ib.flight_number = c.flight_number
    and ib.origin = c.origin and ib.dest = c.dest and ib.sched_dep_utc = c.sched_dep_utc
left join weather_now as wx
    on wx.origin = c.origin and wx.dest = c.dest and wx.prediction_time = c.prediction_time
order by c.year, c.month, c.sched_dep_utc, c.carrier, c.flight_number
