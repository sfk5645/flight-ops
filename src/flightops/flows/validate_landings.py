"""Check tracking against BTS on a day BTS has published, using adsb.lol history as the feed.

    uv run --env-file .env python -m flightops.flows.validate_landings --date 2026-08-18

Downloads the day's adsb.lol archive (~4 GB, streamed, nothing saved to disk), thins it to
5-minute snapshots, runs the same tracking code as the daily job, and prints Markdown tables
comparing it with BTS (see flightops.tracking.validation). Writes nothing to the lake.
Results are recorded in docs/landing_validation.md.
"""

import argparse
import logging
import time
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from flightops.ingestion import adsblol
from flightops.silver.bts import silver_key
from flightops.storage import LakeStorage
from flightops.tracking.daily import AIRLINES_KEY, AIRPORTS_KEY, build_tracked_flights
from flightops.tracking.validation import compare_with_bts

log = logging.getLogger(__name__)


def bts_around(lake: LakeStorage, day: date) -> pa.Table:
    """BTS flights dated the day before through the day after (BTS uses local dates)."""
    days = [day - timedelta(days=1), day, day + timedelta(days=1)]
    months = sorted({(d.year, d.month) for d in days})
    bts = pa.concat_tables(lake.read_parquet(silver_key(*m)) for m in months)
    return bts.filter(pc.is_in(bts.column("flight_date"), pa.array(days, pa.date32())))


def markdown(table: pa.Table) -> str:
    rows = [table.column_names, *zip(*(c.to_pylist() for c in table.columns), strict=True)]
    lines = ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    lines.insert(1, "|" + "---|" * table.num_columns)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="UTC day")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lake = LakeStorage.from_settings()
    started = time.monotonic()
    positions = adsblol.read_day(args.date)
    log.info("%d positions in %.0f s", positions.num_rows, time.monotonic() - started)

    airports, airlines = lake.read_parquet(AIRPORTS_KEY), lake.read_parquet(AIRLINES_KEY)
    flights = build_tracked_flights(positions, airports, airlines, args.date)
    log.info("%d tracked flights", flights.num_rows)

    results = compare_with_bts(flights, bts_around(lake, args.date), args.date)
    print(f"# {args.date}")
    for name, table in results.items():
        print(f"\n## {name}\n\n{markdown(table)}")


if __name__ == "__main__":
    main()
