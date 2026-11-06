"""Reference data (airports, airlines): download snapshots into bronze."""

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from flightops.duck import connect
from flightops.ingestion.download import download
from flightops.storage import LakeStorage, lake_key

log = logging.getLogger(__name__)

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPENFLIGHTS_AIRLINES_URL = (
    "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
)
OPENFLIGHTS_AIRLINE_COLUMNS = [
    "airline_id", "name", "alias", "iata", "icao", "callsign", "country", "active",
]  # fmt: skip


def _read_ourairports(path: Path) -> pa.Table:
    return connect().read_csv(str(path), all_varchar=True).to_arrow_table()


def _read_openflights_airlines(path: Path) -> pa.Table:
    # No header row; "\N" means missing.
    return (
        connect()
        .read_csv(
            str(path),
            header=False,
            names=OPENFLIGHTS_AIRLINE_COLUMNS,
            all_varchar=True,
            na_values=["\\N"],
        )
        .to_arrow_table()
    )


SOURCES = {
    "ourairports_airports": (OURAIRPORTS_URL, _read_ourairports),
    "openflights_airlines": (OPENFLIGHTS_AIRLINES_URL, _read_openflights_airlines),
}


def ingest_snapshots(lake: LakeStorage, snapshot: str | None = None) -> dict[str, str]:
    """Store today's copy of each reference file in bronze, one snapshot per day.

    Snapshots are never overwritten, so we can always see what a table looked like on a date.
    """
    snapshot = snapshot or datetime.now(UTC).date().isoformat()
    keys = {}
    for dataset, (url, reader) in SOURCES.items():
        key = lake_key("bronze", dataset, snapshot=snapshot)
        keys[dataset] = key
        if lake.exists(key):
            log.info("bronze %s already exists, skipping download", key)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            table = reader(download(url, Path(tmp) / "data"))
        lake.write_parquet(key, table)
        log.info("bronze %s: %s rows", key, table.num_rows)
    return keys
