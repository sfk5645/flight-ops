"""Turn airline callsigns into carrier + flight number, the way BTS identifies flights.

    DAL1184 -> airline_icao DAL, carrier DL, flight_number 1184

Only callsigns of three letters followed by 1-4 digits are read as airline flights. Others
(private registrations like N512AB, letter suffixes like DAL118A, military) keep their callsign
with no carrier or flight number.

The ICAO -> IATA step uses CARRIER_CODES first and the OpenFlights airline table second.
OpenFlights is out of date for several US regional airlines (Republic as RW instead of YX,
Envoy and Endeavor missing, Piedmont as the 1989 airline PI), which are among the busiest
callsigns at US airports.
"""

import pyarrow as pa

from flightops.duck import connect

# US carriers, keyed by ICAO callsign prefix. The carrier is the IATA code BTS reports flights
# under (Reporting_Airline), checked against silver/bts_flights for the twelve carriers BTS
# covered in 2026: AA AS B6 DL F9 G4 MQ OH OO UA WN YX. The rest aren't in BTS (anymore).
CARRIER_CODES = {
    "AAL": "AA",  # American
    "ASA": "AS",  # Alaska
    "JBU": "B6",  # JetBlue
    "DAL": "DL",  # Delta
    "FFT": "F9",  # Frontier
    "AAY": "G4",  # Allegiant
    "ENY": "MQ",  # Envoy (American Eagle)
    "JIA": "OH",  # PSA (American Eagle)
    "SKW": "OO",  # SkyWest (United Express, Delta Connection, American Eagle, Alaska)
    "UAL": "UA",  # United
    "SWA": "WN",  # Southwest
    "RPA": "YX",  # Republic (OpenFlights: RW)
    "EDV": "9E",  # Endeavor (Delta Connection), in BTS until 2025
    "PDT": "PT",  # Piedmont (American Eagle)
    "HAL": "HA",  # Hawaiian
    "NKS": "NK",  # Spirit
    "QXE": "QX",  # Horizon
    "ASH": "YV",  # Mesa
    "GJS": "G7",  # GoJet
    "UCA": "C5",  # CommutAir
    "AWI": "ZW",  # Air Wisconsin
    "SCX": "SY",  # Sun Country
    "MXY": "MX",  # Breeze
    "VXP": "XP",  # Avelo
}

_MAPPED = """
with parsed as (
    select
        *,
        regexp_extract(callsign, '^([A-Z]{3})([0-9]{1,4})$', 1) as prefix,
        regexp_extract(callsign, '^([A-Z]{3})([0-9]{1,4})$', 2) as digits
    from flights
),
openflights as (
    -- Several rows can share an ICAO code (renamed or defunct airlines): prefer active ones.
    select icao, arg_min(iata, (not active, airline_id)) as iata
    from airlines
    where icao is not null and iata is not null and length(iata) = 2
    group by icao
)
select
    p.* exclude (prefix, digits),
    nullif(p.prefix, '') as airline_icao,
    coalesce(c.carrier, o.iata) as carrier,
    try_cast(nullif(p.digits, '') as integer) as flight_number
from parsed p
left join carrier_codes c on c.icao = p.prefix
left join openflights o on o.icao = p.prefix
"""


def add_flight_numbers(flights: pa.Table, airlines: pa.Table) -> pa.Table:
    """Add airline_icao, carrier and flight_number columns (null when not an airline flight).

    Row order is kept.
    """
    con = connect()
    con.register("flights", flights.append_column("_row", pa.array(range(flights.num_rows))))
    con.register("airlines", airlines)
    con.register(
        "carrier_codes",
        pa.table({"icao": list(CARRIER_CODES), "carrier": list(CARRIER_CODES.values())}),
    )
    return con.sql(f"select * exclude (_row) from ({_MAPPED}) order by _row").to_arrow_table()
