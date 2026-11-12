select
    airport,
    observed_utc,
    visibility_mi,
    ceiling_ft,
    wind_kt,
    gust_kt,
    weather_codes,
    flight_category,
    thunderstorm,
    snow_or_ice
from {{ source('silver', 'airport_weather') }}
