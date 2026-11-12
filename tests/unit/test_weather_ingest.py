from datetime import date

import pytest

from flightops.ingestion import weather


def test_iem_station_ids() -> None:
    assert weather.iem_station("ORD", "KORD") == "ORD"  # lower 48: drop the K
    assert weather.iem_station("HNL", "PHNL") == "PHNL"  # Hawaii, Alaska, territories keep it
    assert weather.iem_station("FCA", "KGPI") == "GPI"  # IATA and ICAO codes can differ
    assert weather.iem_station("XYZ", None) == "XYZ"


def test_bronze_key() -> None:
    assert weather.bronze_key(2026, 8) == "bronze/iem_metar/year=2026/month=08/data.parquet"


def test_read_csv_keeps_text_and_empty_values() -> None:
    csv = (
        b"station,valid,vsby,skyc1,skyc2,skyc3,skyl1,skyl2,skyl3,wxcodes,sknt,gust\n"
        b"EYW,2026-08-14 00:00,10.00,OVC,,,3300.00,,,VCTS,3.00,\n"
    )
    row = weather.read_csv(csv).to_pylist()[0]
    assert row["station"] == "EYW"
    assert row["vsby"] == "10.00"  # typed in silver, not here
    assert row["wxcodes"] == "VCTS"
    assert row["gust"] == ""


def test_refuses_a_month_that_has_not_settled() -> None:
    class EmptyLake:
        def exists(self, key: str) -> bool:
            return False

    with pytest.raises(ValueError, match="isn't complete"):
        weather.ingest_month(EmptyLake(), 2026, 10, ["ORD"], today=date(2026, 11, 2))  # type: ignore[arg-type]
