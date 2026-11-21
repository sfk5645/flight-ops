{{
    config(
        location=var('training_set_location') or var('lake') ~ '/gold/training_set',
        options={'partition_by': 'year, month', 'overwrite_or_ignore': true},
    )
}}

-- Pre-departure model training rows: one per completed flight, with its inputs and the label
-- (arr_delay_min). Cancelled and diverted flights have no arrival delay; they stay in
-- fct_flights_hist with their status.
--
-- Every input must be known at prediction time (prediction_lead_min before scheduled departure,
-- 1 hour):
--   the flight itself    from the schedule the user enters
--   delay history        only BTS months published by then (see macros/history.sql)
--   airport traffic      takeoffs / landings in the 3 whole hours before the prediction hour
--   today's lateness     how late those takeoffs / landings (and the airline's) ran vs usual
--   inbound aircraft     a guess from the last 7 days of rotations, and where that flight is now
--                        (built separately in intermediate/int_inbound.sql: in one query
--                        with everything else it ran out of memory)
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
        sched_dep_utc - to_minutes({{ var('prediction_lead_min') }}) as prediction_time,
        date_trunc('hour', sched_dep_utc - to_minutes({{ var('prediction_lead_min') }})) as prediction_hour,
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

-- Today's lateness: how late each flight took off and landed compared with its usual time over
-- the previous 7 days (live: the same callsign's takeoffs and landings in our tracking, which
-- knows the departure airport at takeoff and the arrival airport at landing, hence the keys).
-- Times are compared as time of day, wrapping at midnight (23:55 vs 00:05 is 10 minutes), the
-- median over the previous days is taken, and the result is capped at -60..240 minutes so a few
-- extreme cases don't dominate averages. Needs at least 3 previous days.
leg_times as (
    select
        flight_id,
        flight_date,
        hash(carrier, flight_number, origin) as dep_key,
        hash(carrier, flight_number, dest) as arr_key,
        (epoch(takeoff_utc)::bigint // 60 % 1440)::integer as takeoff_tod,
        (epoch(landing_utc)::bigint // 60 % 1440)::integer as landing_tod
    from flights
    where status <> 'cancelled' and takeoff_utc is not null
),

usual_times as (
    select
        flight_id,
        takeoff_tod,
        landing_tod,
        list(takeoff_tod) over (
            partition by dep_key order by flight_date
            range between interval 7 day preceding and interval 1 day preceding
        ) as usual_takeoffs,
        list_filter(
            list(landing_tod) over (
                partition by arr_key order by flight_date
                range between interval 7 day preceding and interval 1 day preceding
            ),
            lambda x: x is not null
        ) as usual_landings
    from leg_times
),

lateness as (
    select
        flight_id,
        case when len(usual_takeoffs) >= 3 then least(greatest(list_aggregate(
            list_transform(usual_takeoffs, lambda x: ((takeoff_tod - x + 720) % 1440 + 1440) % 1440 - 720),
            'median'), -60), 240)
        end as dep_late_min,
        case when len(usual_landings) >= 3 and landing_tod is not null then least(greatest(
            list_aggregate(
                list_transform(usual_landings, lambda x: ((landing_tod - x + 720) % 1440 + 1440) % 1440 - 720),
                'median'), -60), 240)
        end as arr_late_min
    from usual_times
),

-- Airport traffic: how many planes took off from (or landed at) each airport in the 3 whole
-- hours before the prediction hour, and how many is normal for that airport, local weekday and
-- hour. A slowdown (ratio well under 1) is the clearest sign of storms, ground stops or
-- congestion. Live, both the count and its normal come from tracked
-- flights of the same 12 BTS carriers, so the ratio is comparable even though tracking misses
-- about 10% of flights.
movements as (
    select
        f.origin as airport,
        date_trunc('hour', f.takeoff_utc) as hour_utc,
        1 as deps,
        0 as arrs,
        l.dep_late_min as dep_late,
        null::double as arr_late
    from flights as f
    left join lateness as l using (flight_id)
    where f.status <> 'cancelled' and f.takeoff_utc is not null
    union all
    select f.dest, date_trunc('hour', f.landing_utc), 0, 1, null, l.arr_late_min
    from flights as f
    left join lateness as l using (flight_id)
    where f.status = 'completed' and f.landing_utc is not null
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
        sum(coalesce(m.arrs, 0)) over w as arrs_3h,
        sum(m.dep_late_total) over w / nullif(sum(m.dep_late_n) over w, 0) as dep_late_3h,
        sum(m.dep_late15) over w / nullif(sum(m.dep_late_n) over w, 0) as dep_late15_3h,
        sum(m.arr_late_total) over w / nullif(sum(m.arr_late_n) over w, 0) as arr_late_3h,
        sum(m.arr_late15) over w / nullif(sum(m.arr_late_n) over w, 0) as arr_late15_3h
    from airport_hours as h
    left join (
        select
            airport,
            hour_utc,
            sum(deps) as deps,
            sum(arrs) as arrs,
            sum(dep_late) as dep_late_total,
            count(dep_late) as dep_late_n,
            count_if(dep_late > 15) as dep_late15,
            sum(arr_late) as arr_late_total,
            count(arr_late) as arr_late_n,
            count_if(arr_late > 15) as arr_late15
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
    select
        t.airport, t.hour_utc, t.deps_3h, t.arrs_3h, n.deps_3h_normal, n.arrs_3h_normal,
        t.dep_late_3h, t.dep_late15_3h, t.arr_late_3h, t.arr_late15_3h
    from traffic as t
    left join traffic_normal as n using (airport, weekday_local, hour_local, month_index)
),

-- The airline's lateness across the whole network in the same 3 hours: catches airline-wide
-- problems (IT outages, crew shortages) that no single airport shows.
carrier_hourly as (
    select
        f.carrier,
        date_trunc('hour', f.takeoff_utc) as hour_utc,
        sum(l.dep_late_min) as total,
        count(l.dep_late_min) as n,
        count_if(l.dep_late_min > 15) as late15
    from flights as f
    join lateness as l using (flight_id)
    group by all
),

carrier_now as (
    select
        t.carrier,
        t.prediction_hour,
        sum(h.total) / nullif(sum(h.n), 0) as dep_late_3h,
        sum(h.late15) / nullif(sum(h.n), 0) as dep_late15_3h
    from (select distinct carrier, prediction_hour from completed) as t
    join carrier_hourly as h
        on h.carrier = t.carrier
        and h.hour_utc >= t.prediction_hour - interval 3 hour
        and h.hour_utc < t.prediction_hour
    group by all
),

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

    -- today's lateness in the same 3 hours: average minutes late vs usual, share > 15 late
    round(ot.dep_late_3h, 1)::float as origin_dep_late_3h,
    round(ot.dep_late15_3h, 3)::float as origin_dep_late15_3h,
    round(dt.arr_late_3h, 1)::float as dest_arr_late_3h,
    round(dt.arr_late15_3h, 3)::float as dest_arr_late15_3h,
    round(cn.dep_late_3h, 1)::float as carrier_dep_late_3h,
    round(cn.dep_late15_3h, 3)::float as carrier_dep_late15_3h,

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
left join carrier_now as cn
    on cn.carrier = c.carrier and cn.prediction_hour = c.prediction_hour
left join {{ ref('int_inbound') }} as ib
    on ib.flight_key = hash(c.carrier, c.flight_number, c.origin, c.dest)
    and ib.sched_dep_utc = c.sched_dep_utc
left join weather_now as wx
    on wx.origin = c.origin and wx.dest = c.dest and wx.prediction_time = c.prediction_time
order by c.year, c.month, c.sched_dep_utc, c.carrier, c.flight_number
