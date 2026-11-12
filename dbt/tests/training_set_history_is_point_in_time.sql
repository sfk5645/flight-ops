-- Point-in-time check: recompute inputs for a sample of training rows straight from
-- fct_flights_hist and require an exact match:
--   route and carrier history  only flights dated in the 3 published months (M-5 .. M-3 for a
--                              flight in month M)
--   origin takeoffs            only takeoffs in the 3 whole hours before the prediction hour
--                              (prediction = 2 hours before scheduled departure)
--   inbound pattern            only rotations from the 7 days before, whose later leg took off
--                              before the prediction time
--   origin weather             the latest report at least 10 minutes before the prediction time
-- A feature that leaked newer flights (for example the flight's own month, or takeoffs after
-- the prediction time) would differ.
with sample as (
    select * from {{ ref('training_set') }}
    where route_delay_3m is not null
    using sample 300 rows (reservoir, 7)
),

windows as (
    select
        *,
        (date_trunc('month', flight_date) - to_months({{ var('bts_lag_months') }} + 2))::date
            as window_start,
        (date_trunc('month', flight_date) - to_months({{ var('bts_lag_months') }} - 1))::date
            as window_end,
        date_trunc('hour', sched_dep_utc - interval 2 hour) as prediction_hour
    from sample
),

recomputed as (
    select
        w.carrier,
        w.flight_number,
        w.sched_dep_utc,
        w.route_delay_3m,
        w.carrier_delay_3m,
        (
            select avg(f.arr_delay_min) from {{ ref('fct_flights_hist') }} as f
            where f.status = 'completed' and f.origin = w.origin and f.dest = w.dest
                and f.flight_date >= w.window_start and f.flight_date < w.window_end
        ) as route_check,
        (
            select avg(f.arr_delay_min) from {{ ref('fct_flights_hist') }} as f
            where f.status = 'completed' and f.carrier = w.carrier
                and f.flight_date >= w.window_start and f.flight_date < w.window_end
        ) as carrier_check,
        w.origin_deps_3h,
        (
            select count(*) from {{ ref('fct_flights_hist') }} as f
            where f.status <> 'cancelled' and f.taxi_out_min is not null and f.origin = w.origin
                and f.dep_utc + to_minutes(f.taxi_out_min) >= w.prediction_hour - interval 3 hour
                and f.dep_utc + to_minutes(f.taxi_out_min) < w.prediction_hour
        ) as origin_deps_check,
        w.inbound_days_same,
        (
            select max(days) from (
                select count(distinct f.flight_date) as days
                from {{ ref('fct_rotations') }} as r
                join {{ ref('fct_flights_hist') }} as f on f.flight_id = r.flight_id
                join {{ ref('fct_flights_hist') }} as p on p.flight_id = r.prev_flight_id
                where r.chain_ok and f.carrier = w.carrier and f.flight_number = w.flight_number
                    and f.origin = w.origin and f.dest = w.dest
                    and f.flight_date between w.flight_date - 7 and w.flight_date - 1
                    and f.dep_utc + to_minutes(f.taxi_out_min) < w.sched_dep_utc - interval 2 hour
                group by p.carrier, p.flight_number, p.origin
            )
        ) as inbound_days_check,
        w.origin_wx_age_min,
        (
            select date_diff('minute', max(x.observed_utc), w.sched_dep_utc - interval 2 hour)
            from {{ ref('stg_airport_weather') }} as x
            where x.airport = w.origin
                and x.observed_utc <= w.sched_dep_utc - interval 2 hour - interval 10 minute
        ) as origin_wx_age_check
    from windows as w
)

select *
from recomputed
-- history is stored rounded to 0.1 minutes
where abs(route_delay_3m - route_check) > 0.051
    or abs(carrier_delay_3m - carrier_check) > 0.051
    or route_check is null
    or origin_deps_3h is distinct from origin_deps_check
    or inbound_days_same is distinct from inbound_days_check
    or origin_wx_age_min is distinct from origin_wx_age_check
