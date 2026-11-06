import zipfile
from pathlib import Path

from flightops.flows.bts_history import months
from flightops.ingestion.bts import bronze_key, read_zip, zip_url


def test_urls_and_keys_for_a_month() -> None:
    assert zip_url(2026, 8).endswith("_1987_present_2026_8.zip")
    assert bronze_key(2026, 8) == "bronze/bts_ontime/year=2026/month=08/data.parquet"


def test_read_zip_keeps_text_and_drops_trailing_empty_column(tmp_path: Path) -> None:
    # BTS lines end with a trailing comma.
    csv = '"FlightDate","Cancelled","CRSDepTime",\n2026-08-01,0.00,0005,\n'
    zip_path = tmp_path / "bts.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("On_Time_2026_8.csv", csv)
        archive.writestr("readme.html", "<html></html>")

    table = read_zip(zip_path)
    assert table.column_names == ["FlightDate", "Cancelled", "CRSDepTime"]
    assert table.to_pylist() == [
        {"FlightDate": "2026-08-01", "Cancelled": "0.00", "CRSDepTime": "0005"}
    ]


def test_months_crosses_year_boundary() -> None:
    assert list(months("2025-11", "2026-02")) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]
