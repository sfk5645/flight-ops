import pyarrow as pa
import pytest

from flightops.config import R2Settings
from flightops.storage import LakeStorage, lake_key


def test_lake_key_builds_hive_partitions() -> None:
    key = lake_key("bronze", "bts_ontime", year="2024", month="01")
    assert key == "bronze/bts_ontime/year=2024/month=01/data.parquet"


def test_parquet_round_trip(lake: LakeStorage) -> None:
    table = pa.table({"flight": ["DL1184", "UA22"], "arr_delay_min": [12, -4]})
    lake.write_parquet("silver/test/data.parquet", table)
    assert lake.read_parquet("silver/test/data.parquet").equals(table)


def test_write_refuses_to_overwrite_by_default(lake: LakeStorage) -> None:
    table = pa.table({"x": [1]})
    lake.write_parquet("bronze/test/data.parquet", table)
    with pytest.raises(FileExistsError):
        lake.write_parquet("bronze/test/data.parquet", table)


def test_write_overwrites_when_asked(lake: LakeStorage) -> None:
    lake.write_parquet("gold/test/data.parquet", pa.table({"x": [1]}))
    lake.write_parquet("gold/test/data.parquet", pa.table({"x": [2]}), overwrite=True)
    assert lake.read_parquet("gold/test/data.parquet").column("x").to_pylist() == [2]


def test_read_many_stacks_files_in_order(lake: LakeStorage) -> None:
    keys = [f"bronze/test/{n}.parquet" for n in range(5)]
    for n, key in enumerate(keys):
        lake.write_parquet(key, pa.table({"n": [n], "other": ["x"]}))
    table = lake.read_parquet_many(keys[::-1], columns=["n"])
    assert table.column_names == ["n"]
    assert table.column("n").to_pylist() == [4, 3, 2, 1, 0]


def test_exists_and_list_keys(lake: LakeStorage) -> None:
    assert not lake.exists("bronze/a/data.parquet")
    for name in ["a", "b"]:
        lake.write_parquet(f"bronze/{name}/data.parquet", pa.table({"x": [1]}))
    lake.write_parquet("silver/a/data.parquet", pa.table({"x": [1]}))

    assert lake.exists("bronze/a/data.parquet")
    assert sorted(lake.list_keys("bronze/")) == ["bronze/a/data.parquet", "bronze/b/data.parquet"]


def test_r2_endpoint_from_account_id() -> None:
    settings = R2Settings(account_id="abc123", access_key_id="k", secret_access_key="s")
    assert settings.endpoint_url == "https://abc123.r2.cloudflarestorage.com"
