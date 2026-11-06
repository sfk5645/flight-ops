"""BTS On-Time Performance: download a month's zip and store it unchanged in bronze."""

import logging
import tempfile
import zipfile
from pathlib import Path

import pyarrow as pa

from flightops.duck import connect
from flightops.ingestion.download import download
from flightops.storage import LakeStorage, lake_key

log = logging.getLogger(__name__)

ZIP_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)


def zip_url(year: int, month: int) -> str:
    return ZIP_URL.format(year=year, month=month)


def bronze_key(year: int, month: int) -> str:
    return lake_key("bronze", "bts_ontime", year=str(year), month=f"{month:02d}")


def read_zip(zip_path: Path) -> pa.Table:
    """Read the CSV inside a BTS zip with every column as text.

    Bronze keeps values exactly as BTS wrote them ("8.00", "2400", ...); typing happens in silver,
    where bad values can be quarantined instead of crashing the load.
    """
    with zipfile.ZipFile(zip_path) as archive, tempfile.TemporaryDirectory() as tmp:
        (csv_name,) = [name for name in archive.namelist() if name.endswith(".csv")]
        csv_path = archive.extract(csv_name, tmp)
        table = connect().read_csv(csv_path, all_varchar=True).to_arrow_table()
    # Each CSV line ends with a trailing comma, which readers turn into an empty, unnamed column.
    empty = [name for name in table.column_names if name.startswith("column")]
    return table.drop_columns(empty)


def ingest_month(lake: LakeStorage, year: int, month: int) -> str:
    """Download one month into bronze. Skips months already there, so re-running is safe."""
    key = bronze_key(year, month)
    if lake.exists(key):
        log.info("bronze %s already exists, skipping download", key)
        return key
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = download(zip_url(year, month), Path(tmp) / "bts.zip")
        table = read_zip(zip_path)
    lake.write_parquet(key, table)
    log.info("bronze %s: %s rows", key, table.num_rows)
    return key
