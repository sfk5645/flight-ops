from datetime import date, timedelta
from typing import Any

import pyarrow as pa

from flightops.flows.backfill_tracks import backfill, chunks, flights_key, points_key
from flightops.ingestion.adsblol import POSITIONS_SCHEMA
from flightops.storage import LakeStorage
from flightops.tracking.daily import AIRLINES_KEY, AIRPORTS_KEY
from tests.unit.test_daily import AIRLINES, AIRPORTS, DAY, at, flight


def fake_adsblol(*tracks: list[dict[str, Any]]) -> tuple[Any, list[date]]:
    """A read_day that serves each UTC day's rows of `tracks`, and the days it was asked for."""
    rows = [row for track in tracks for row in track]
    asked: list[date] = []

    def read_day(day: date) -> pa.Table:
        asked.append(day)
        first = at(24 * (day - DAY).days)
        on_day = [r for r in rows if first <= r["snapshot_time"] < first + 86400]
        return pa.Table.from_pylist(on_day, schema=POSITIONS_SCHEMA)

    return read_day, asked


def test_builds_each_day_with_its_neighbours_and_downloads_each_archive_once(
    lake: LakeStorage,
) -> None:
    lake.write_parquet(AIRPORTS_KEY, AIRPORTS, overwrite=True)
    lake.write_parquet(AIRLINES_KEY, AIRLINES, overwrite=True)
    read_day, asked = fake_adsblol(
        flight("b00002", depart=-2, arrive=0.75),  # took off the evening before DAY
        flight("a00001", depart=9, arrive=11),
        flight("c00003", depart=23.5, arrive=25.5),  # lands after midnight
        flight("d00004", depart=33, arrive=35),  # DAY + 1, 09:00
    )
    second = DAY + timedelta(days=1)
    backfill(lake, DAY, second, read_day)

    assert asked == [DAY - timedelta(days=1), DAY, second, second + timedelta(days=1)]
    first_day = lake.read_parquet(flights_key(DAY)).to_pylist()
    assert {f["icao24"] for f in first_day} == {"a00001", "c00003"}
    late = next(f for f in first_day if f["icao24"] == "c00003")
    assert (late["dest"], late["dest_how"]) == ("LGA", "seen")
    assert {p["icao24"] for p in lake.read_parquet(points_key(DAY)).to_pylist()} == {
        "a00001",
        "c00003",
    }
    assert [f["icao24"] for f in lake.read_parquet(flights_key(second)).to_pylist()] == ["d00004"]


def test_chunks_cover_the_range_without_overlap() -> None:
    assert chunks(date(2026, 10, 22), date(2026, 11, 3), size=6) == [
        {"start": "2026-10-22", "end": "2026-10-27"},
        {"start": "2026-10-28", "end": "2026-11-02"},
        {"start": "2026-11-03", "end": "2026-11-03"},
    ]
