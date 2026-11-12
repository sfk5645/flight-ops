from typing import Any

import pyarrow as pa

from flightops.silver.weather import BRONZE_COLUMNS, build_silver, station_lookup

AIRPORTS = pa.table({"iata": ["ORD", "HNL", None], "icao": ["KORD", "PHNL", "KXYZ"]})

BASE = {
    "station": "ORD",
    "valid": "2026-08-14 12:51",
    "vsby": "10.00",
    "skyc1": "FEW",
    "skyc2": "",
    "skyc3": "",
    "skyl1": "25000.00",
    "skyl2": "",
    "skyl3": "",
    "wxcodes": "",
    "sknt": "8.00",
    "gust": "",
}


def report(**changes: str) -> dict[str, str]:
    return {**BASE, **changes}


def build(*rows: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bronze = pa.table({c: [r[c] for r in rows] for c in BRONZE_COLUMNS})
    result = build_silver(bronze, AIRPORTS)
    return result.valid.to_pylist(), result.rejected.to_pylist()


def test_station_lookup_maps_iem_ids_back_to_iata() -> None:
    assert station_lookup(AIRPORTS).to_pylist() == [
        {"station": "ORD", "iata": "ORD"},
        {"station": "PHNL", "iata": "HNL"},
    ]


def test_clear_report_is_typed_and_vfr() -> None:
    (row,), rejected = build(report())
    assert rejected == []
    assert row["airport"] == "ORD"
    assert row["observed_utc"].isoformat() == "2026-08-14T12:51:00+00:00"
    assert row["visibility_mi"] == 10.0
    assert row["ceiling_ft"] is None  # FEW clouds are not a ceiling
    assert row["wind_kt"] == 8
    assert row["gust_kt"] is None
    assert row["flight_category"] == "VFR"
    assert row["thunderstorm"] is False
    assert row["snow_or_ice"] is False


def test_flight_category_uses_the_lowest_broken_or_overcast_layer() -> None:
    rows, _ = build(
        # scattered at 400 ft doesn't count; broken at 800 does -> IFR
        report(valid="2026-08-14 01:00", skyc1="SCT", skyl1="400", skyc2="BKN", skyl2="800"),
        report(valid="2026-08-14 02:00", skyc1="OVC", skyl1="3000"),  # MVFR at exactly 3,000
        report(valid="2026-08-14 03:00", vsby="0.50"),  # LIFR from visibility alone
        report(valid="2026-08-14 04:00", skyc1="VV", skyl1="200"),  # sky obscured
        report(valid="2026-08-14 05:00", vsby=""),  # no visibility: unknown, not VFR
    )
    assert [(r["ceiling_ft"], r["flight_category"]) for r in rows] == [
        (800, "IFR"),
        (3000, "MVFR"),
        (None, "LIFR"),
        (200, "LIFR"),
        (None, None),
    ]


def test_weather_codes_set_the_flags() -> None:
    rows, _ = build(
        report(valid="2026-08-14 01:00", wxcodes="+TSRA BR", gust="35.00"),
        report(valid="2026-08-14 02:00", wxcodes="VCTS"),
        report(valid="2026-08-14 03:00", wxcodes="-FZRA"),
        report(valid="2026-08-14 04:00", wxcodes="-SN"),
        report(valid="2026-08-14 05:00", wxcodes="-RA"),
    )
    assert [(r["thunderstorm"], r["snow_or_ice"]) for r in rows] == [
        (True, False),
        (True, False),
        (False, True),
        (False, True),
        (False, False),
    ]
    assert rows[0]["gust_kt"] == 35


def test_bad_reports_go_to_quarantine_with_a_reason() -> None:
    valid, rejected = build(
        report(),
        report(),  # exact duplicate
        report(station="ZZZ"),
        report(valid="yesterday"),
        report(valid="2026-08-14 13:00", vsby="", skyc1="", skyl1="", sknt=""),
        report(valid="2026-08-14 14:00", sknt="calm"),
        report(valid="2026-08-14 15:00", sknt="400.00"),
        report(valid="2026-08-14 16:00", skyc1="OVC", skyl1=""),  # ceiling with no height
    )
    assert len(valid) == 1
    assert sorted(r["reject_reason"] for r in rejected) == [
        "duplicate",
        "empty_report",
        "invalid_number",
        "invalid_number",
        "invalid_time",
        "unknown_station",
        "wind_out_of_range",
    ]
    assert rejected[0]["vsby"] == "10.00"  # original text kept


def test_duplicates_keep_the_most_complete_copy() -> None:
    (row,), _ = build(report(vsby=""), report())
    assert row["visibility_mi"] == 10.0
