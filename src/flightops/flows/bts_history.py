"""Load BTS history into the lake: reference tables, then bronze and silver for each month.

    uv run --env-file .env python -m flightops.flows.bts_history --start 2024-09 --end 2026-08

Safe to re-run: bronze months already downloaded are skipped, silver is rebuilt deterministically.
"""

import argparse
import logging
from collections.abc import Iterator

from flightops.ingestion import bts as bts_ingest
from flightops.ingestion import reference as reference_ingest
from flightops.silver import bts as bts_silver
from flightops.silver import reference as reference_silver
from flightops.storage import LakeStorage, lake_key

log = logging.getLogger(__name__)

AIRPORTS_KEY = lake_key("silver", "airports")
AIRLINES_KEY = lake_key("silver", "airlines")


def months(start: str, end: str) -> Iterator[tuple[int, int]]:
    """Yield (year, month) from start to end inclusive; both formatted YYYY-MM."""
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def refresh_reference(lake: LakeStorage) -> None:
    keys = reference_ingest.ingest_snapshots(lake)
    airports = reference_silver.build_airports(lake.read_parquet(keys["ourairports_airports"]))
    lake.write_parquet(AIRPORTS_KEY, airports, overwrite=True)
    airlines = reference_silver.build_airlines(lake.read_parquet(keys["openflights_airlines"]))
    lake.write_parquet(AIRLINES_KEY, airlines, overwrite=True)
    log.info("silver airports: %s rows, airlines: %s rows", airports.num_rows, airlines.num_rows)


def load_month(lake: LakeStorage, year: int, month: int) -> bts_silver.SilverResult:
    bronze_key = bts_ingest.ingest_month(lake, year, month)
    result = bts_silver.build_silver(
        lake.read_parquet(bronze_key, columns=bts_silver.BRONZE_COLUMNS),
        lake.read_parquet(AIRPORTS_KEY),
    )
    lake.write_parquet(bts_silver.silver_key(year, month), result.valid, overwrite=True)
    # Written even when empty, so a re-run that fixes a problem also clears old rejects.
    lake.write_parquet(bts_silver.quarantine_key(year, month), result.rejected, overwrite=True)
    log.info(
        "%d-%02d: %s rows to silver, %s quarantined",
        year,
        month,
        result.valid.num_rows,
        result.rejected.num_rows,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="first month, YYYY-MM")
    parser.add_argument("--end", required=True, help="last month, YYYY-MM")
    parser.add_argument(
        "--skip-reference", action="store_true", help="reuse the existing silver airports table"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lake = LakeStorage.from_settings()
    if not args.skip_reference:
        refresh_reference(lake)
    for year, month in months(args.start, args.end):
        load_month(lake, year, month)


if __name__ == "__main__":
    main()
