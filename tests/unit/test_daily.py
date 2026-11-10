from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest

from flightops.ingestion.opensky import SCHEMA, snapshot_key
from flightops.storage import LakeStorage
from flightops.tracking.daily import AIRPORTS_KEY, compact_day, snapshot_keys, tracked_flights_key

DAY = date(2026, 11, 9)
MIDNIGHT = datetime(2026, 11, 9, tzinfo=UTC)

ATL = (33.6367, -84.4281)
LGA = (40.7772, -73.8726)

AIRPORTS = pa.table(
    {
        "iata": ["ATL", "LGA"],
        "type": ["large_airport", "large_airport"],
        "latitude": [ATL[0], LGA[0]],
        "longitude": [ATL[1], LGA[1]],
        "elevation_ft": [1026, 21],
    }
)


def at(hours: float) -> int:
    """Seconds since 1970 for `hours` after midnight starting DAY (negative = the day before)."""
    return int((MIDNIGHT + timedelta(hours=hours)).timestamp())


def flight(icao24: str, depart: float, arrive: float) -> list[dict[str, Any]]:
    """Snapshots every 5 minutes: on the ground at ATL, flying to LGA, on the ground at LGA."""
    rows = []
    start, end = at(depart - 0.25), at(arrive + 0.25)  # 15 minutes on the ground each end
    for t in range(start, end + 1, 300):
        airborne = at(depart) <= t <= at(arrive)
        share = min(max((t - at(depart)) / (at(arrive) - at(depart)), 0), 1)
        lat = ATL[0] + share * (LGA[0] - ATL[0])
        lon = ATL[1] + share * (LGA[1] - ATL[1])
        rows.append(position(icao24, t, lat, lon, airborne))
    return rows


def position(
    icao24: str, t: int, lat: float, lon: float, airborne: bool, altitude: float = 10000.0
) -> dict[str, Any]:
    return {
        "snapshot_time": t,
        "icao24": icao24,
        "callsign": "DAL1184 ",
        "time_position": t,
        "latitude": lat,
        "longitude": lon,
        "on_ground": not airborne,
        "baro_altitude": altitude if airborne else None,
        "velocity": 220.0 if airborne else 5.0,
        "true_track": 45.0,
        "vertical_rate": 0.0 if airborne else None,
    }


def store(lake: LakeStorage, *tracks: list[dict[str, Any]]) -> None:
    by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in (row for track in tracks for row in track):
        by_time[row["snapshot_time"]].append(row)
    for t, rows in by_time.items():
        lake.write_parquet(snapshot_key(t), pa.Table.from_pylist(rows, schema=SCHEMA))
    lake.write_parquet(AIRPORTS_KEY, AIRPORTS, overwrite=True)


def test_reads_from_twelve_hours_before_through_the_next_day(lake: LakeStorage) -> None:
    hours = [-18, -11, 10, 25, 49]  # D-1 06:00, D-1 13:00, D 10:00, D+1 01:00, D+2 01:00
    store(lake, *[[position("a1b2c3", at(h), *ATL, airborne=False)] for h in hours])
    assert snapshot_keys(lake, DAY) == [snapshot_key(at(h)) for h in [-11, 10, 25]]


def test_keeps_the_flights_that_took_off_that_day(lake: LakeStorage) -> None:
    store(
        lake,
        flight("a00001", depart=9, arrive=11),  # an ordinary daytime flight
        flight("b00002", depart=-2, arrive=0.75),  # took off the evening before
        flight("c00003", depart=23.75, arrive=25),  # lands after midnight
    )
    flights = {f["icao24"]: f for f in compact_day(lake, DAY).to_pylist()}

    assert set(flights) == {"a00001", "c00003"}
    assert (flights["a00001"]["origin"], flights["a00001"]["dest"]) == ("ATL", "LGA")
    late = flights["c00003"]
    assert (late["end_reason"], late["dest"]) == ("landing_seen", "LGA")
    assert late["first_ground_time"] == MIDNIGHT + timedelta(hours=25, minutes=5)


def test_morning_takeoff_uses_last_nights_landing(lake: LakeStorage) -> None:
    # Landed at LGA last night; first seen this morning already at cruise, nothing to project.
    evening = flight("e00005", depart=-6, arrive=-4)
    morning = [position("e00005", at(8 + m / 60), *ATL, airborne=True) for m in range(0, 60, 5)]
    store(lake, evening, morning)
    (leg,) = compact_day(lake, DAY).to_pylist()
    assert (leg["origin"], leg["origin_how"]) == ("LGA", "previous_leg")


def test_rerunning_a_day_gives_the_same_file(lake: LakeStorage) -> None:
    store(lake, flight("a00001", depart=9, arrive=11))
    first = compact_day(lake, DAY)
    assert compact_day(lake, DAY).equals(first)
    assert lake.read_parquet(tracked_flights_key(DAY)).equals(first)


def test_a_day_without_snapshots_is_an_error(lake: LakeStorage) -> None:
    store(lake, flight("a00001", depart=-30, arrive=-28))
    with pytest.raises(FileNotFoundError, match="no snapshots for 2026-11-09"):
        compact_day(lake, DAY)
