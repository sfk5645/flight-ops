"""Backfill tracked flights and track points from adsb.lol history (in-flight model examples).

    uv run --env-file .env python -m flightops.flows.backfill_tracks \
        --start 2026-10-22 --end 2026-10-27
    uv run python -m flightops.flows.backfill_tracks --plan --start ... --end ...  # chunks

Our own collection only keeps track points from the day it started (raw OpenSky snapshots
expire after 3 days), so a month of training examples would take a month to gather. adsb.lol
publishes every past day, and the same tracking code turns it into the same two tables.

Each day's archive (~4 GB) is streamed and thinned to 5-minute snapshots as it downloads
(see flightops.ingestion.adsblol); nothing is written to disk. Building day D needs the
evening before (where morning flights came from) and the day after (landings after
midnight), like the daily job, so a run over N days downloads N + 2 archives and keeps at
most three days of thinned positions in memory. Runs in GitHub Actions (backfill.yml), split
into chunks of a few days that run side by side.

Stored apart from the OpenSky tables (silver/adsblol_tracked_flights, silver/adsblol_track_points)
because the feed differs: adsb.lol positions are fresher (~27 s old vs ~112 s) and cover
different airports, so training can tell the sources apart and check the model on OpenSky-like
data, which is what it sees live. Kept 90 days like the daily tables (prune_lake).
Re-running a day rebuilds the same files and replaces them.
"""

import argparse
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from flightops.ingestion import adsblol
from flightops.storage import LakeStorage, lake_key
from flightops.tracking.daily import (
    AIRLINES_KEY,
    AIRPORTS_KEY,
    LOOKBACK,
    build_tracked_flights,
    start_of,
)
from flightops.tracking.tracks import track_points

log = logging.getLogger(__name__)

CHUNK_DAYS = 6


def flights_key(day: date) -> str:
    return lake_key("silver", "adsblol_tracked_flights", date=day.isoformat())


def points_key(day: date) -> str:
    return lake_key("silver", "adsblol_track_points", date=day.isoformat())


def days(start: date, end: date) -> Iterator[date]:
    for n in range((end - start).days + 1):
        yield start + timedelta(days=n)


def chunks(start: date, end: date, size: int = CHUNK_DAYS) -> list[dict[str, str]]:
    """[start, end] cut into runs of `size` days, for the workflow's job matrix."""
    all_days = list(days(start, end))
    return [
        {"start": part[0].isoformat(), "end": part[-1].isoformat()}
        for part in (all_days[i : i + size] for i in range(0, len(all_days), size))
    ]


def backfill(
    lake: LakeStorage,
    start: date,
    end: date,
    read_day: Callable[[date], pa.Table] = adsblol.read_day,
) -> None:
    airports, airlines = lake.read_parquet(AIRPORTS_KEY), lake.read_parquet(AIRLINES_KEY)
    loaded: dict[date, pa.Table] = {}
    for day in days(start, end):
        for needed in (day - timedelta(days=1), day, day + timedelta(days=1)):
            if needed not in loaded:
                started = time.monotonic()
                loaded[needed] = read_day(needed)
                log.info(
                    "adsb.lol %s: %d positions in %.0f s",
                    needed,
                    loaded[needed].num_rows,
                    time.monotonic() - started,
                )
        for old in [d for d in loaded if d < day - timedelta(days=1)]:
            del loaded[old]

        evening_before = loaded[day - timedelta(days=1)]
        from_s = int((start_of(day) - LOOKBACK).timestamp())
        evening_before = evening_before.filter(
            pc.greater_equal(evening_before.column("snapshot_time"), from_s)
        )
        positions = pa.concat_tables([evening_before, loaded[day], loaded[day + timedelta(days=1)]])
        flights = build_tracked_flights(positions, airports, airlines, day)
        points = track_points(positions, flights)
        lake.write_parquet(flights_key(day), flights, overwrite=True)
        lake.write_parquet(points_key(day), points, overwrite=True)
        log.info("%s: %d flights, %d track points", day, flights.num_rows, points.num_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="first UTC day")
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="last UTC day")
    parser.add_argument("--plan", action="store_true", help="print the chunks as JSON and stop")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end is before --start")
    if args.plan:
        print(json.dumps(chunks(args.start, args.end)))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backfill(LakeStorage.from_settings(), args.start, args.end)


if __name__ == "__main__":
    main()
