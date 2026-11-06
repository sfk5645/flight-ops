"""BTS flights, bronze → silver.

Every bronze row ends up in exactly one place:
- silver: typed, times in UTC, passed every contract rule
- quarantine: the original text values plus the first rule it broke (reject_reason)

Time zones: BTS times are local "hhmm" strings with no time zone. Only the departure is
converted (using the origin airport's zone); every other timestamp is derived from it with
the minute counts BTS reports (scheduled duration, delays, taxi times). That avoids guessing
whether an arrival landed the next day. The scheduled local arrival time is then used as a
consistency check: converting our UTC arrival back to the destination's zone must reproduce it.
"""

from dataclasses import dataclass

import pyarrow as pa

from flightops.duck import connect
from flightops.storage import lake_key

BRONZE_COLUMNS = [
    "FlightDate",
    "Reporting_Airline",
    "Tail_Number",
    "Flight_Number_Reporting_Airline",
    "Origin",
    "Dest",
    "CRSDepTime",
    "CRSArrTime",
    "CRSElapsedTime",
    "DepDelay",
    "ArrDelay",
    "TaxiOut",
    "TaxiIn",
    "ActualElapsedTime",
    "AirTime",
    "Distance",
    "Cancelled",
    "CancellationCode",
    "Diverted",
    "CarrierDelay",
    "WeatherDelay",
    "NASDelay",
    "SecurityDelay",
    "LateAircraftDelay",
]

# Delays outside this range (minutes) are treated as data errors. Real extreme delays reach
# roughly 2-3 days; anything earlier than 2 hours is not a real "early" departure or arrival.
MIN_DELAY = -120
MAX_DELAY = 4320

# Airports whose IATA code changed. BTS uses whichever code was current on the flight date;
# silver always uses today's code, so a route keeps one identity across the change.
# A new rename shows up in quarantine as unknown_origin_airport / unknown_dest_airport.
AIRPORT_CODE_ALIASES = {
    "PBI": "DJT",  # Palm Beach International, renamed and re-coded
}


def silver_key(year: int, month: int) -> str:
    return lake_key("silver", "bts_flights", year=str(year), month=f"{month:02d}")


def quarantine_key(year: int, month: int) -> str:
    return lake_key("quarantine", "bts_flights", year=str(year), month=f"{month:02d}")


@dataclass(frozen=True)
class SilverResult:
    valid: pa.Table
    rejected: pa.Table


_PARSE = """
create temp table parsed as
select
    b.*,
    try_strptime(b.raw_FlightDate, '%Y-%m-%d')::date as flight_date,
    nullif(trim(b.raw_Reporting_Airline), '') as carrier,
    nullif(trim(b.raw_Tail_Number), '') as tail_number,
    try_cast(b.raw_Flight_Number_Reporting_Airline as integer) as flight_number,
    -- "hhmm" -> minutes after local midnight; "2400" means midnight at the end of the day.
    try_cast(b.raw_CRSDepTime as integer) as sched_dep_hhmm,
    case
        when regexp_full_match(b.raw_CRSDepTime, '[0-9]{4}')
            and sched_dep_hhmm <= 2400
            and sched_dep_hhmm % 100 < 60
        then sched_dep_hhmm // 100 * 60 + sched_dep_hhmm % 100
    end as sched_dep_minute,
    -- Numeric fields arrive as text like "12.00".
    try_cast(try_cast(b.raw_CRSElapsedTime as double) as integer) as sched_elapsed_min,
    try_cast(try_cast(b.raw_DepDelay as double) as integer) as dep_delay_min,
    try_cast(try_cast(b.raw_ArrDelay as double) as integer) as arr_delay_min,
    try_cast(try_cast(b.raw_TaxiOut as double) as integer) as taxi_out_min,
    try_cast(try_cast(b.raw_TaxiIn as double) as integer) as taxi_in_min,
    try_cast(try_cast(b.raw_ActualElapsedTime as double) as integer) as actual_elapsed_min,
    try_cast(try_cast(b.raw_AirTime as double) as integer) as air_time_min,
    try_cast(try_cast(b.raw_Distance as double) as integer) as distance_mi,
    try_cast(b.raw_Cancelled as double) = 1 as cancelled,
    try_cast(b.raw_Diverted as double) = 1 as diverted,
    nullif(trim(b.raw_CancellationCode), '') as cancellation_code,
    try_cast(try_cast(b.raw_CarrierDelay as double) as integer) as carrier_delay_min,
    try_cast(try_cast(b.raw_WeatherDelay as double) as integer) as weather_delay_min,
    try_cast(try_cast(b.raw_NASDelay as double) as integer) as nas_delay_min,
    try_cast(try_cast(b.raw_SecurityDelay as double) as integer) as security_delay_min,
    try_cast(try_cast(b.raw_LateAircraftDelay as double) as integer) as late_aircraft_delay_min,
    coalesce(oa.current_code, b.raw_Origin) as origin,
    coalesce(da.current_code, b.raw_Dest) as dest,
    o.tz as origin_tz,
    d.tz as dest_tz
from bronze b
left join airport_aliases oa on oa.old_code = b.raw_Origin
left join airport_aliases da on da.old_code = b.raw_Dest
left join airports o on o.iata = coalesce(oa.current_code, b.raw_Origin)
left join airports d on d.iata = coalesce(da.current_code, b.raw_Dest)
"""

_TIME = """
create temp table timed as
with dep as (
    select
        *,
        timezone(origin_tz, flight_date + to_minutes(sched_dep_minute)) as sched_dep_utc
    from parsed
)
select
    *,
    sched_dep_utc + to_minutes(sched_elapsed_min) as sched_arr_utc
from dep
"""

_CHECK = f"""
create temp table checked as
select
    *,
    case
        when flight_date is null then 'invalid_flight_date'
        when carrier is null then 'missing_carrier'
        when flight_number is null then 'invalid_flight_number'
        when origin_tz is null then 'unknown_origin_airport'
        when dest_tz is null then 'unknown_dest_airport'
        when sched_dep_minute is null then 'invalid_sched_dep_time'
        when sched_elapsed_min is null or sched_elapsed_min <= 0 then 'invalid_sched_elapsed_time'
        when strftime(timezone(dest_tz, sched_arr_utc), '%H%M')
            <> replace(raw_CRSArrTime, '2400', '0000') then 'schedule_inconsistent'
        when cancelled is null or diverted is null then 'invalid_status_flags'
        when cancelled and diverted then 'cancelled_and_diverted'
        when not cancelled and dep_delay_min is null then 'missing_dep_delay'
        when not cancelled and not diverted and arr_delay_min is null then 'missing_arr_delay'
        when dep_delay_min not between {MIN_DELAY} and {MAX_DELAY}
            or arr_delay_min not between {MIN_DELAY} and {MAX_DELAY} then 'delay_out_of_range'
    end as reject_reason
from timed
"""

_VALID = """
select
    -- Destination is part of the key: one flight number can cover several legs from the same
    -- origin at the same time (e.g. JFK->CVG and the through trip JFK->LAS).
    -- Stored as the raw 16-byte md5 rather than its 32-character hex text: hashes don't
    -- compress, so this halves the biggest column. hex(flight_id) shows it as text.
    unhex(md5(concat_ws('|', carrier, flight_number, origin, dest, epoch(sched_dep_utc))))
        as flight_id,
    flight_date,
    carrier,
    flight_number,
    tail_number,
    origin,
    dest,
    case when cancelled then 'cancelled' when diverted then 'diverted' else 'completed' end
        as status,
    sched_dep_utc,
    sched_arr_utc,
    sched_dep_utc + to_minutes(dep_delay_min) as dep_utc,
    sched_arr_utc + to_minutes(arr_delay_min) as arr_utc,
    dep_delay_min,
    arr_delay_min,
    taxi_out_min,
    taxi_in_min,
    sched_elapsed_min,
    actual_elapsed_min,
    air_time_min,
    distance_mi,
    cancellation_code,
    carrier_delay_min,
    weather_delay_min,
    nas_delay_min,
    security_delay_min,
    late_aircraft_delay_min
from checked
where reject_reason is null
order by sched_dep_utc, carrier, flight_number, origin
"""

_REJECTED = """
select reject_reason, {columns}
from checked
where reject_reason is not null
order by reject_reason, raw_FlightDate, raw_Reporting_Airline,
    raw_Flight_Number_Reporting_Airline, raw_Origin, raw_CRSDepTime
"""


def build_silver(bronze: pa.Table, airports: pa.Table) -> SilverResult:
    """Type, convert to UTC and validate one month of bronze flights.

    Output is sorted, so the same input always produces byte-identical files (idempotency).
    """
    con = connect()
    # Prefix raw text columns: DuckDB names are case-insensitive, so raw "Cancelled" would
    # otherwise collide with the typed "cancelled" column.
    raw = bronze.select(BRONZE_COLUMNS).rename_columns([f"raw_{c}" for c in BRONZE_COLUMNS])
    con.register("bronze", raw)
    con.register("airports", airports.select(["iata", "tz"]))
    con.register(
        "airport_aliases",
        pa.table(
            {
                "old_code": list(AIRPORT_CODE_ALIASES),
                "current_code": list(AIRPORT_CODE_ALIASES.values()),
            }
        ),
    )
    for statement in (_PARSE, _TIME, _CHECK):
        con.execute(statement)
    columns = ", ".join(f"raw_{name} as {name}" for name in BRONZE_COLUMNS)
    return SilverResult(
        valid=con.sql(_VALID).to_arrow_table(),
        rejected=con.sql(_REJECTED.format(columns=columns)).to_arrow_table(),
    )
