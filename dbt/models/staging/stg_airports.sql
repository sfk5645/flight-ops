select
    iata,
    icao,
    name,
    municipality,
    iso_country,
    iso_region,
    type,
    scheduled_service,
    latitude,
    longitude,
    elevation_ft,
    tz
from {{ source('silver', 'airports') }}
