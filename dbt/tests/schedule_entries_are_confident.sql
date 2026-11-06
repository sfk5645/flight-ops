-- A timetable entry's flight must have been scheduled at least the minimum number of times, and
-- its chosen time can't be seen more often than the flight was scheduled.
select carrier, flight_number, route, times_seen, times_scheduled
from {{ ref('dim_schedule') }}
where times_scheduled < {{ var('schedule_min_times_seen') }}
   or times_seen > times_scheduled
