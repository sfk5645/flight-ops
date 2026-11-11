from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa

from flightops.tracking.validation import compare_with_bts

DAY = date(2026, 8, 18)
MIDNIGHT = datetime(2026, 8, 18, tzinfo=UTC)


def at(hours: float) -> datetime:
    return MIDNIGHT + timedelta(hours=hours)


def bts_flight(number: int, depart: float, carrier: str = "DL") -> dict[str, Any]:
    """Pushed back at `depart` (hours), 15 min taxi out, 2 h in the air, 10 min taxi in."""
    return {
        "carrier": carrier,
        "flight_number": number,
        "origin": "ATL",
        "dest": "LGA",
        "status": "completed",
        "dep_utc": at(depart),
        "arr_utc": at(depart + 2.25 + 10 / 60),
        "taxi_out_min": 15,
        "taxi_in_min": 10,
    }


def tracked(
    number: int,
    takeoff: float,
    *,
    carrier: str = "DL",
    dest: str | None = "LGA",
    landing_late_min: float = 0,
) -> dict[str, Any]:
    return {
        "carrier": carrier,
        "flight_number": number,
        "origin": "ATL",
        "dest": dest,
        "origin_how": "seen",
        "dest_how": "seen" if dest else None,
        "first_air_time": at(takeoff),
        "takeoff_time": at(takeoff),
        "landing_time": at(takeoff + 2) + timedelta(minutes=landing_late_min) if dest else None,
    }


def compare(bts: list[dict[str, Any]], flights: list[dict[str, Any]]) -> dict[str, list[Any]]:
    tables = compare_with_bts(pa.Table.from_pylist(flights), pa.Table.from_pylist(bts), DAY)
    return {name: table.to_pylist() for name, table in tables.items()}


def test_found_confirmed_and_timing() -> None:
    result = compare(
        bts=[
            bts_flight(100, depart=9),  # tracked: wheels off 09:15, on 11:15
            bts_flight(200, depart=10),  # never tracked
            bts_flight(300, depart=12),  # tracked, landing airport unknown
            bts_flight(400, depart=21),  # after the graded window
        ],
        flights=[
            tracked(100, takeoff=9.25, landing_late_min=1),
            tracked(300, takeoff=12.25, dest=None),
            tracked(9, takeoff=14, carrier="DL"),  # not in BTS
        ],
    )
    assert result["found"][0] == {
        "carrier": "all",
        "bts_flights": 3,
        "found": 1,
        "found_pct": 33.3,
    }
    assert result["confirmed"][0] == {"how": "all", "tracked": 2, "in_bts_pct": 50.0}
    landing = next(r for r in result["timing"] if r["event"] == "landing")
    assert (landing["flights"], landing["median_min"]) == (1, 1.0)
    assert {r["reason"]: r["flights"] for r in result["misses"]} == {
        "no leg with that flight number": 1,
        "landing airport unknown": 1,
    }


def test_takeoff_unknown_matches_on_first_sighting() -> None:
    flight = tracked(100, takeoff=9.5) | {"origin_how": "previous_leg", "takeoff_time": None}
    result = compare(bts=[bts_flight(100, depart=9)], flights=[flight])
    assert result["found"][0]["found"] == 1
