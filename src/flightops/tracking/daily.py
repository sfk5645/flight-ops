"""One day of tracked flights: snapshots -> legs -> airports -> flight numbers -> silver.

A flight belongs to the UTC day it took off (its first airborne sighting). To build day D:
- Snapshots from LOOKBACK before D are read too. A plane already flying at midnight then starts
  its leg before D and is left out (it belongs to D-1), and morning departures can find where
  the plane landed the evening before (origin_how = 'previous_leg').
- Snapshots after D are read up to whatever exists, so flights still in the air at midnight get
  their landing. Run it once every flight that took off on D has landed: the longest flights
  are ~19 hours, so the evening of D+1 (raw snapshots are kept 3 days, see docs/setup.md).

Re-running a day rebuilds the same file from the same snapshots and replaces it.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from flightops.ingestion.opensky import DATASET
from flightops.storage import LakeStorage, lake_key
from flightops.tracking.airports import match_airports
from flightops.tracking.callsigns import add_flight_numbers
from flightops.tracking.segments import POSITION_COLUMNS, split_legs

log = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=12)
AIRPORTS_KEY = lake_key("silver", "airports")
AIRLINES_KEY = lake_key("silver", "airlines")

# 288 five-minute snapshots make a full day; fewer means runs were missed.
SNAPSHOTS_PER_DAY = 288


def tracked_flights_key(day: date) -> str:
    return lake_key("silver", "tracked_flights", date=day.isoformat())


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def snapshot_keys(lake: LakeStorage, day: date) -> list[str]:
    """Snapshot keys from LOOKBACK before `day` through the end of the next day, in time order."""
    first = int((_start_of(day) - LOOKBACK).timestamp())
    keys = []
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        prefix = f"bronze/{DATASET}/date={d.isoformat()}/"
        keys += [k for k in lake.list_keys(prefix) if _snapshot_time(k) >= first]
    return sorted(keys, key=_snapshot_time)


def _snapshot_time(key: str) -> int:
    """bronze/opensky_states/date=2026-11-09/1794182425.parquet -> 1794182425"""
    return int(key.rsplit("/", 1)[1].removesuffix(".parquet"))


def build_tracked_flights(
    positions: pa.Table, airports: pa.Table, airlines: pa.Table, day: date
) -> pa.Table:
    """Legs with airports, times and flight numbers for the flights that took off on `day`."""
    flights = add_flight_numbers(match_airports(split_legs(positions), airports), airlines)
    start, end = _start_of(day), _start_of(day + timedelta(days=1))
    took_off = flights.column("first_air_time")
    keep = pc.and_(pc.greater_equal(took_off, start), pc.less(took_off, end))
    return flights.filter(keep)


def compact_day(lake: LakeStorage, day: date) -> pa.Table:
    """Build and store `day`'s tracked flights. Returns the stored table."""
    keys = snapshot_keys(lake, day)
    on_day = sum(f"/date={day.isoformat()}/" in k for k in keys)
    if on_day == 0:
        raise FileNotFoundError(f"no snapshots for {day}")
    if on_day < SNAPSHOTS_PER_DAY * 0.9:
        log.warning("only %d of %d snapshots for %s", on_day, SNAPSHOTS_PER_DAY, day)

    positions = lake.read_parquet_many(keys, columns=POSITION_COLUMNS)
    airports, airlines = lake.read_parquet(AIRPORTS_KEY), lake.read_parquet(AIRLINES_KEY)
    flights = build_tracked_flights(positions, airports, airlines, day)
    lake.write_parquet(tracked_flights_key(day), flights, overwrite=True)

    still_flying = pc.sum(pc.equal(flights.column("end_reason"), "still_flying")).as_py() or 0
    log.info(
        "%s: %d flights from %d snapshots (%d on the day), %d still flying at the end of the data",
        day,
        flights.num_rows,
        len(keys),
        on_day,
        still_flying,
    )
    return flights
