"""Silver reference tables: airports (with IANA time zones) and airlines."""

import pyarrow as pa
from timezonefinder import TimezoneFinder

from flightops.duck import connect

# When several OurAirports rows share an IATA code (e.g. a closed field and its replacement),
# keep the busiest-looking one.
_AIRPORT_TYPE_RANK = """
    case type
        when 'large_airport' then 1 when 'medium_airport' then 2 when 'small_airport' then 3
        when 'closed' then 9 else 5
    end
"""


def build_airports(ourairports: pa.Table) -> pa.Table:
    """One row per IATA code, with the airport's time zone derived from its coordinates.

    OurAirports has no time zone column, so we look each airport's lat/lon up in the
    world time zone boundaries (timezonefinder). That covers every airport, including
    ones missing from older lists that do include time zones.
    """
    con = connect()
    con.register("raw", ourairports)
    airports = con.sql(f"""
        select
            iata_code as iata,
            nullif(icao_code, '') as icao,
            name,
            municipality,
            iso_country,
            iso_region,
            type,
            scheduled_service = 'yes' as scheduled_service,
            latitude_deg::double as latitude,
            longitude_deg::double as longitude,
            try_cast(elevation_ft as integer) as elevation_ft
        from raw
        where iata_code is not null and iata_code <> ''
        qualify row_number() over (
            partition by iata_code
            order by {_AIRPORT_TYPE_RANK}, scheduled_service desc, ident
        ) = 1
        order by iata
    """).to_arrow_table()

    finder = TimezoneFinder()
    tz = [
        finder.timezone_at(lat=lat, lng=lon)
        for lat, lon in zip(
            airports.column("latitude").to_pylist(),
            airports.column("longitude").to_pylist(),
            strict=True,
        )
    ]
    return airports.append_column("tz", pa.array(tz, pa.string()))


def build_airlines(openflights: pa.Table) -> pa.Table:
    """Airline codes and radio callsigns, used later to map callsigns like DAL1184 to DL 1184."""
    con = connect()
    con.register("raw", openflights)
    return con.sql("""
        select
            try_cast(airline_id as integer) as airline_id,
            name,
            nullif(nullif(iata, ''), '-') as iata,
            nullif(nullif(icao, ''), 'N/A') as icao,
            nullif(callsign, '') as callsign,
            country,
            active = 'Y' as active
        from raw
        where try_cast(airline_id as integer) > 0
        order by airline_id
    """).to_arrow_table()
