"""Load airport weather history: bronze (one request per month, for the airports BTS flew to
that month), then silver (typed, validated, with the weather flags).

    uv run --env-file .env python -m flightops.flows.weather_history --start 2024-09 --end 2026-08

Safe to re-run: months already in bronze are skipped, silver is rebuilt deterministically.
"""

import argparse
import logging
from datetime import UTC, datetime

import pyarrow as pa

from flightops.flows.bts_history import AIRPORTS_KEY, months
from flightops.ingestion import weather
from flightops.silver import weather as weather_silver
from flightops.silver.bts import silver_key
from flightops.storage import LakeStorage

log = logging.getLogger(__name__)


def stations_for_month(lake: LakeStorage, airports: pa.Table, year: int, month: int) -> list[str]:
    """IEM station ids of every airport with a BTS flight in the month."""
    flights = lake.read_parquet(silver_key(year, month), columns=["origin", "dest"])
    iata = set(flights.column("origin").to_pylist()) | set(flights.column("dest").to_pylist())
    icao = dict(zip(airports["iata"].to_pylist(), airports["icao"].to_pylist(), strict=True))
    return sorted({weather.iem_station(code, icao.get(code)) for code in iata})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="first month, YYYY-MM")
    parser.add_argument("--end", required=True, help="last month, YYYY-MM")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lake = LakeStorage.from_settings()
    today = datetime.now(UTC).date()
    airports = lake.read_parquet(AIRPORTS_KEY, columns=["iata", "icao"])
    for year, month in months(args.start, args.end):
        stations = stations_for_month(lake, airports, year, month)
        bronze_key = weather.ingest_month(lake, year, month, stations, today=today)
        result = weather_silver.build_silver(lake.read_parquet(bronze_key), airports)
        lake.write_parquet(weather_silver.silver_key(year, month), result.valid, overwrite=True)
        # Written even when empty, so a re-run that fixes a problem also clears old rejects.
        lake.write_parquet(
            weather_silver.quarantine_key(year, month), result.rejected, overwrite=True
        )
        log.info(
            "%d-%02d: %s reports to silver, %s quarantined",
            year,
            month,
            result.valid.num_rows,
            result.rejected.num_rows,
        )


if __name__ == "__main__":
    main()
