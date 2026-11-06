{{
    config(
        location=var('lake') ~ '/gold/fct_rotations',
        options={'partition_by': 'year, month', 'overwrite_or_ignore': true},
    )
}}

-- One row per flown leg (completed or diverted, with a tail number), linked to the same
-- aircraft's previous leg. Delay models use it for the inbound-aircraft features (how late the
-- plane is coming in, and how much turnaround slack there was).
--
-- Only the link and what it adds are stored; everything else about either leg (route, times,
-- delays) is a join to fct_flights_hist on flight_id / prev_flight_id. That keeps the table small.
--
-- Legs are ordered by actual departure: the physical order the plane flew them. Cancelled
-- flights are skipped because they don't move the plane.
--
-- chain_ok marks rows where "previous leg" is trustworthy:
--   - the previous leg landed (completed) at this leg's origin
--   - the schedule planned it that way (previous scheduled arrival before this scheduled
--     departure); otherwise the plane was most likely swapped in at the last minute
--   - the plane landed before this leg took off (a few BTS tail numbers break this)
with legs as (
    select
        flight_id,
        tail_number,
        origin,
        dest,
        status,
        sched_dep_utc,
        sched_arr_utc,
        dep_utc,
        arr_utc,
        arr_delay_min,
        year,
        month
    from {{ ref('fct_flights_hist') }}
    where status <> 'cancelled' and tail_number is not null
),

chained as (
    select
        *,
        lag(flight_id) over plane as prev_flight_id,
        lag(dest) over plane as prev_dest,
        lag(status) over plane as prev_status,
        lag(sched_arr_utc) over plane as prev_sched_arr_utc,
        lag(arr_utc) over plane as prev_arr_utc,
        lag(arr_delay_min) over plane as prev_arr_delay_min
    from legs
    window plane as (partition by tail_number order by dep_utc, sched_dep_utc, flight_id)
)

select
    flight_id,
    tail_number,
    prev_flight_id,
    prev_arr_delay_min,
    date_diff('minute', prev_sched_arr_utc, sched_dep_utc) as sched_turn_min,
    date_diff('minute', prev_arr_utc, sched_dep_utc) as actual_turn_min,
    coalesce(
        prev_dest = origin
        and prev_status = 'completed'
        and prev_sched_arr_utc < sched_dep_utc
        and prev_arr_utc <= dep_utc,
        false
    ) as chain_ok,
    year,
    month
from chained
order by year, month, sched_dep_utc, flight_id
