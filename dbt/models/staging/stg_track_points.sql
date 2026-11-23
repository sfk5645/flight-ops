-- Track points from both feeds (see stg_tracked_flights for `feed`).
{% for feed, table in [('opensky', 'track_points'), ('adsblol', 'adsblol_track_points')] %}
select
    '{{ feed }}' as feed,
    icao24,
    first_air_time,
    position_time,
    latitude,
    longitude,
    altitude_m,
    speed_ms,
    vertical_rate_ms,
    track_deg
from {{ source('silver', table) }}
{% if not loop.last %}union all{% endif %}
{% endfor %}
