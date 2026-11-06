"""Run a month through the lake (bronze already present) and check re-runs are identical."""

import pyarrow as pa

from flightops.flows.bts_history import AIRPORTS_KEY, load_month
from flightops.ingestion.bts import bronze_key
from flightops.silver.bts import quarantine_key, silver_key
from flightops.storage import LakeStorage
from tests.unit.test_bts_silver import AIRPORTS, bronze


def raw_bytes(lake: LakeStorage, key: str) -> bytes:
    return lake._client.get_object(Bucket=lake.bucket, Key=key)["Body"].read()


def test_rerunning_a_month_gives_identical_files(lake: LakeStorage) -> None:
    month = bronze({}, {"Dest": "XXX"}, {"Flight_Number_Reporting_Airline": "7"})
    # Extra columns are ignored, as with the real 109-column files.
    month = month.append_column("Div5TailNum", pa.array([None] * 3, pa.string()))
    lake.write_parquet(bronze_key(2026, 8), month)
    lake.write_parquet(AIRPORTS_KEY, AIRPORTS)

    first = load_month(lake, 2026, 8)
    silver_first = raw_bytes(lake, silver_key(2026, 8))
    quarantine_first = raw_bytes(lake, quarantine_key(2026, 8))

    load_month(lake, 2026, 8)
    assert raw_bytes(lake, silver_key(2026, 8)) == silver_first
    assert raw_bytes(lake, quarantine_key(2026, 8)) == quarantine_first
    assert (first.valid.num_rows, first.rejected.num_rows) == (2, 1)
