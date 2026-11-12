"""Airport weather reports, bronze → silver.

Every bronze row ends up in exactly one place:
- silver: one row per airport and report time, typed, with the weather flags the models use
- quarantine: the original text values plus the first rule it broke (reject_reason)

The flags are computed here, per report, and nowhere else: the live job will put
aviationweather.gov reports through this same function, so "thunderstorm" or "IFR" means
exactly the same thing in training and in live predictions.

Flight category (FAA definitions), from the ceiling (lowest broken, overcast or obscured
layer) and visibility:
    LIFR  ceiling < 500 ft  or visibility < 1 mile
    IFR   ceiling < 1,000   or visibility < 3
    MVFR  ceiling <= 3,000  or visibility <= 5
    VFR   otherwise
Lower categories mean fewer arrivals per hour at most airports, so they come with ground delays.
"""

import pyarrow as pa

from flightops.duck import connect
from flightops.ingestion.weather import FIELDS, iem_station
from flightops.silver.bts import SilverResult
from flightops.storage import lake_key

BRONZE_COLUMNS = ["station", "valid", *FIELDS]

# Readings outside these ranges are instrument or transcription errors.
MAX_VISIBILITY_MI = 100  # stations report at most 10 (US) or ~6 (metric); 100 is clearly wrong
MAX_WIND_KT = 150


def silver_key(year: int, month: int) -> str:
    return lake_key("silver", "airport_weather", year=str(year), month=f"{month:02d}")


def quarantine_key(year: int, month: int) -> str:
    return lake_key("quarantine", "airport_weather", year=str(year), month=f"{month:02d}")


_PARSE = """
create temp table parsed as
select
    b.*,
    s.iata as airport,
    try_strptime(b.raw_valid, '%Y-%m-%d %H:%M') as valid_naive,
    timezone('UTC', valid_naive) as observed_utc,
    try_cast(nullif(b.raw_vsby, '') as double) as visibility_mi,
    try_cast(nullif(b.raw_sknt, '') as double) as wind_kt_raw,
    try_cast(nullif(b.raw_gust, '') as double) as gust_kt_raw,
    nullif(trim(b.raw_wxcodes), '') as weather_codes,
    -- each cloud layer: cover (FEW/SCT/BKN/OVC, VV = sky obscured) and its base in feet
    [
        {'cover': trim(b.raw_skyc1), 'base': try_cast(nullif(b.raw_skyl1, '') as double)},
        {'cover': trim(b.raw_skyc2), 'base': try_cast(nullif(b.raw_skyl2, '') as double)},
        {'cover': trim(b.raw_skyc3), 'base': try_cast(nullif(b.raw_skyl3, '') as double)}
    ] as layers,
    trim(b.raw_skyc1) <> '' as sky_reported,
    list_min([l.base for l in layers if l.cover in ('BKN', 'OVC', 'VV')]) as ceiling_ft_raw,
    -- a field that has text but isn't a number
    (b.raw_vsby <> '' and visibility_mi is null)
        or (b.raw_sknt <> '' and wind_kt_raw is null)
        or (b.raw_gust <> '' and gust_kt_raw is null)
        or len([l for l in layers if l.cover in ('BKN', 'OVC', 'VV') and l.base is null]) > 0
        as bad_number,
    b.raw_vsby = '' and b.raw_skyc1 = '' and b.raw_wxcodes = '' and b.raw_sknt = ''
        as empty_report
from bronze as b
left join stations as s on s.station = b.raw_station
"""

_CHECK = f"""
create temp table checked as
select
    *,
    case
        when airport is null then 'unknown_station'
        when observed_utc is null then 'invalid_time'
        when empty_report then 'empty_report'
        when bad_number then 'invalid_number'
        when visibility_mi not between 0 and {MAX_VISIBILITY_MI} then 'visibility_out_of_range'
        when wind_kt_raw not between 0 and {MAX_WIND_KT}
            or gust_kt_raw not between 0 and {MAX_WIND_KT} then 'wind_out_of_range'
        when ceiling_ft_raw < 0 then 'ceiling_out_of_range'
        -- the same report can arrive twice (for example a corrected copy); keep the most
        -- complete one, then the first by text so reruns pick the same row
        when row_number() over (
            partition by airport, observed_utc
            order by (raw_vsby <> '')::int + (raw_skyc1 <> '')::int + (raw_wxcodes <> '')::int
                + (raw_sknt <> '')::int desc,
                raw_vsby, raw_skyc1, raw_skyl1, raw_wxcodes, raw_sknt, raw_gust, raw_station
        ) > 1 then 'duplicate'
    end as reject_reason
from parsed
"""

_VALID = """
select
    airport,
    observed_utc,
    visibility_mi::float as visibility_mi,
    ceiling_ft_raw::integer as ceiling_ft,
    wind_kt_raw::smallint as wind_kt,
    gust_kt_raw::smallint as gust_kt,
    weather_codes,
    case
        when visibility_mi is null or not sky_reported then null
        when ceiling_ft_raw < 500 or visibility_mi < 1 then 'LIFR'
        when ceiling_ft_raw < 1000 or visibility_mi < 3 then 'IFR'
        when ceiling_ft_raw <= 3000 or visibility_mi <= 5 then 'MVFR'
        else 'VFR'
    end as flight_category,
    -- TS = thunderstorm at the airport, VCTS = in the vicinity (within ~10 miles)
    coalesce(weather_codes like '%TS%', false) as thunderstorm,
    -- snow, snow grains, ice pellets, ice crystals, small hail, anything freezing: de-icing
    coalesce(regexp_matches(weather_codes, 'SN|SG|PL|IC|GS|FZ'), false) as snow_or_ice
from checked
where reject_reason is null
order by airport, observed_utc
"""

_REJECTED = """
select reject_reason, {columns}
from checked
where reject_reason is not null
order by reject_reason, raw_station, raw_valid, raw_vsby, raw_skyc1, raw_wxcodes, raw_sknt
"""


def station_lookup(airports: pa.Table) -> pa.Table:
    """IEM station id -> IATA code, for every airport in the reference table."""
    pairs = {
        iem_station(iata, icao): iata
        for iata, icao in zip(
            airports.column("iata").to_pylist(), airports.column("icao").to_pylist(), strict=True
        )
        if iata
    }
    return pa.table({"station": list(pairs), "iata": list(pairs.values())})


def build_silver(bronze: pa.Table, airports: pa.Table) -> SilverResult:
    """Type, validate and flag one month of reports. Sorted, so reruns give identical files."""
    con = connect()
    # Prefix raw text columns so they can't collide with the typed ones.
    raw = bronze.select(BRONZE_COLUMNS).rename_columns([f"raw_{c}" for c in BRONZE_COLUMNS])
    con.register("bronze", raw)
    con.register("stations", station_lookup(airports))
    for statement in (_PARSE, _CHECK):
        con.execute(statement)
    columns = ", ".join(f"raw_{name} as {name}" for name in BRONZE_COLUMNS)
    return SilverResult(
        valid=con.sql(_VALID).to_arrow_table(),
        rejected=con.sql(_REJECTED.format(columns=columns)).to_arrow_table(),
    )
