-- Tracked flights from both feeds. `feed` says which: 'opensky' is our own live tracking (what
-- the model sees in production), 'adsblol' the history backfill (fresher positions, different
-- receivers), so the two are never mixed when counting or guessing within a feed.
{% for feed, table in [('opensky', 'tracked_flights'), ('adsblol', 'adsblol_tracked_flights')] %}
select
    '{{ feed }}' as feed,
    icao24,
    callsign,
    carrier,
    origin,
    origin_how,
    dest,
    dest_how,
    first_air_time,
    takeoff_time,
    landing_time
from {{ source('silver', table) }}
{% if not loop.last %}union all{% endif %}
{% endfor %}
