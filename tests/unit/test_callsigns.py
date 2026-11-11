from typing import Any

import pyarrow as pa
import pytest

from flightops.tracking.callsigns import add_flight_numbers

AIRLINES = pa.table(
    {
        "airline_id": [1, 2, 3, 4, 5],
        "icao": ["DAL", "RPA", "BAW", "BAW", "EJA"],
        "iata": ["DL", "RW", "BA", "XX", "1I"],
        "active": [True, True, True, False, True],
    }
)


def mapped(*callsigns: str | None) -> list[dict[str, Any]]:
    flights = pa.table({"callsign": pa.array(callsigns, pa.string())})
    return add_flight_numbers(flights, AIRLINES).to_pylist()


@pytest.mark.parametrize(
    ("callsign", "airline_icao", "carrier", "flight_number"),
    [
        ("DAL1184", "DAL", "DL", 1184),
        ("DAL0118", "DAL", "DL", 118),  # leading zero: BTS stores numbers
        ("SKW5712", "SKW", "OO", 5712),  # regional carrier
        ("RPA4410", "RPA", "YX", 4410),  # our table wins over OpenFlights (RW)
        ("ENY3899", "ENY", "MQ", 3899),  # missing from OpenFlights
        ("BAW12", "BAW", "BA", 12),  # OpenFlights fallback, active row preferred
        ("EJA512", "EJA", "1I", 512),  # not an airline in BTS, still mapped
        ("ZZZ123", "ZZZ", None, 123),  # unknown operator
    ],
)
def test_airline_callsigns(
    callsign: str, airline_icao: str, carrier: str | None, flight_number: int
) -> None:
    (row,) = mapped(callsign)
    assert (row["airline_icao"], row["carrier"], row["flight_number"]) == (
        airline_icao,
        carrier,
        flight_number,
    )


@pytest.mark.parametrize("callsign", ["N512AB", "DAL118A", "DAL12345", "VAMPYR01", None])
def test_other_callsigns_get_no_flight(callsign: str | None) -> None:
    (row,) = mapped(callsign)
    assert row["callsign"] == callsign
    assert (row["airline_icao"], row["carrier"], row["flight_number"]) == (None, None, None)


def test_rows_and_order_are_kept() -> None:
    rows = mapped("UAL22", None, "DAL1184", "UAL22")
    assert [r["callsign"] for r in rows] == ["UAL22", None, "DAL1184", "UAL22"]
