from datetime import date

import pyarrow as pa
import pytest

from flightops import retention
from flightops.retention import expired_day_keys, expired_month_keys, prune
from flightops.storage import LakeStorage


def month_keys(prefix: str, months: list[tuple[int, int]], padded: bool = True) -> list[str]:
    fmt = "{:02d}" if padded else "{}"
    return [f"{prefix}year={y}/month={fmt.format(m)}/data.parquet" for y, m in months]


def test_keeps_newest_months_counted_from_the_data_not_today() -> None:
    keys = month_keys("silver/bts_flights/", [(2024, 11), (2024, 12), (2025, 1), (2025, 2)])
    assert expired_month_keys(keys, keep=2) == keys[:2]


def test_unpadded_months_sort_by_number_not_text() -> None:
    # dbt writes month=8 and month=10; as text "10" < "8", which would delete the wrong month.
    keys = month_keys("gold/fct_rotations/", [(2026, 8), (2026, 9), (2026, 10)], padded=False)
    assert expired_month_keys(keys, keep=2) == [keys[0]]


def test_nothing_expires_when_within_the_window() -> None:
    keys = month_keys("silver/bts_flights/", [(2025, 1), (2025, 2)])
    assert expired_month_keys(keys, keep=24) == []


def test_files_outside_partition_folders_are_never_deleted() -> None:
    keys = ["silver/bts_flights/README.txt", "silver/bts_flights/year=2020/notes.txt"]
    assert expired_month_keys(keys, keep=1) == []
    assert expired_day_keys(keys, keep=1, today=date(2026, 11, 6)) == []


def test_daily_window_includes_today() -> None:
    keys = [
        f"silver/flights_live/date=2026-11-0{d}/data.parquet" for d in (3, 4, 5, 6)
    ]  # fmt: skip
    assert expired_day_keys(keys, keep=2, today=date(2026, 11, 6)) == keys[:2]


@pytest.mark.parametrize("dry_run", [True, False])
def test_prune_deletes_only_expired_months(
    lake: LakeStorage, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    monkeypatch.setattr(retention, "MONTHLY_RULES", {"silver/bts_flights/": 2})
    keys = month_keys("silver/bts_flights/", [(2026, 6), (2026, 7), (2026, 8)])
    for key in [*keys, "silver/airports/data.parquet"]:
        lake.write_parquet(key, pa.table({"x": [1]}))

    deleted = prune(lake, date(2026, 11, 6), dry_run=dry_run)

    assert deleted == [keys[0]]
    assert lake.exists(keys[0]) is dry_run
    assert lake.exists(keys[1]) and lake.exists(keys[2])
    assert lake.exists("silver/airports/data.parquet")
