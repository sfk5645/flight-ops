{#
    Infer a timetable from a set of past flights (columns as in int_scheduled_flights).

    For each (carrier, flight_number, origin, dest) seen at least `schedule_min_times_seen` times,
    the schedule is the most recently flown (departure, arrival) local time pair.

    Why "most recent" and not "most common", and why no weekday in the key: measured on the
    backtest (schedule_match_rate). Airlines retime flights by a few minutes every few weeks
    (DL1184 went 05:40 -> 05:51 -> 05:49 over June-July 2026), so the most common time over 60 days
    is often an old one; and some carriers (Southwest) move flight numbers between weekdays, so
    a weekday key leaves too few sightings per key.

    `partition_by` adds extra key columns, so one query can infer several timetables at once
    (the backtest in schedule_match_rate builds one per test month).
#}
{% macro infer_schedule(flights, partition_by=[]) %}
{%- set key = (partition_by + ['carrier', 'flight_number', 'origin', 'dest']) | join(', ') -%}
select * exclude (rank)
from (
    select
        *,
        sum(times_seen) over (partition by {{ key }}) as times_scheduled,
        row_number() over (
            partition by {{ key }}
            order by last_seen_date desc, times_seen desc, dep_minute_local, arr_minute_local
        ) as rank
    from (
        select
            {{ key }},
            dep_minute_local,
            arr_minute_local,
            arr_day_offset,
            count(*) as times_seen,
            min(flight_date) as first_seen_date,
            max(flight_date) as last_seen_date
        from {{ flights }}
        group by all
    )
)
where rank = 1 and times_scheduled >= {{ var('schedule_min_times_seen') }}
{% endmacro %}


{# Minutes between two clock times (minutes after midnight), going round midnight: 23:58 vs 00:03 is 5. #}
{% macro minutes_apart(a, b) -%}
least(abs({{ a }} - {{ b }}), 1440 - abs({{ a }} - {{ b }}))
{%- endmacro %}
