from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest

from flightops.silver.bts import BRONZE_COLUMNS, build_silver

AIRPORTS = pa.table(
    {
        "iata": ["JFK", "LAX", "HNL", "DJT"],
        "tz": ["America/New_York", "America/Los_Angeles", "Pacific/Honolulu", "America/New_York"],
    }
)

# JFK -> LAX, 08:00 EDT (12:00 UTC), 6 hours, lands 11:00 PDT (18:00 UTC).
BASE_ROW = {
    "FlightDate": "2026-08-01",
    "Reporting_Airline": "DL",
    "Tail_Number": "N123DL",
    "Flight_Number_Reporting_Airline": "100",
    "Origin": "JFK",
    "Dest": "LAX",
    "CRSDepTime": "0800",
    "CRSArrTime": "1100",
    "CRSElapsedTime": "360.00",
    "DepDelay": "10.00",
    "ArrDelay": "5.00",
    "TaxiOut": "20.00",
    "TaxiIn": "8.00",
    "ActualElapsedTime": "355.00",
    "AirTime": "327.00",
    "Distance": "2475.00",
    "Cancelled": "0.00",
    "CancellationCode": None,
    "Diverted": "0.00",
    "CarrierDelay": None,
    "WeatherDelay": None,
    "NASDelay": None,
    "SecurityDelay": None,
    "LateAircraftDelay": None,
}


def bronze(*overrides: dict[str, Any]) -> pa.Table:
    rows = [BASE_ROW | override for override in overrides]
    return pa.table({c: pa.array([r[c] for r in rows], pa.string()) for c in BRONZE_COLUMNS})


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def only_valid_row(**override: Any) -> dict[str, Any]:
    result = build_silver(bronze(override), AIRPORTS)
    assert result.rejected.num_rows == 0, result.rejected.to_pylist()
    (row,) = result.valid.to_pylist()
    return row


def test_converts_local_times_to_utc() -> None:
    row = only_valid_row()
    assert row["sched_dep_utc"] == utc(2026, 8, 1, 12, 0)
    assert row["sched_arr_utc"] == utc(2026, 8, 1, 18, 0)
    assert row["dep_utc"] == utc(2026, 8, 1, 12, 10)
    assert row["arr_utc"] == utc(2026, 8, 1, 18, 5)
    assert row["status"] == "completed"
    assert row["arr_delay_min"] == 5


def test_red_eye_lands_next_day() -> None:
    # LAX 23:30 PDT = 06:30 UTC on Aug 2; 5.5 hours later is 08:00 EDT at JFK.
    row = only_valid_row(
        Origin="LAX", Dest="JFK", CRSDepTime="2330", CRSArrTime="0800", CRSElapsedTime="330.00"
    )
    assert row["flight_date"].isoformat() == "2026-08-01"
    assert row["sched_dep_utc"] == utc(2026, 8, 2, 6, 30)
    assert row["sched_arr_utc"] == utc(2026, 8, 2, 12, 0)


def test_2400_means_midnight_at_end_of_day() -> None:
    # 24:00 EDT on Aug 1 = 04:00 UTC on Aug 2; 6 hours later is 03:00 PDT on Aug 2.
    row = only_valid_row(CRSDepTime="2400", CRSArrTime="0300")
    assert row["sched_dep_utc"] == utc(2026, 8, 2, 4, 0)


def test_cancelled_and_diverted_flights_are_kept_with_status() -> None:
    result = build_silver(
        bronze(
            {
                "Cancelled": "1.00",
                "CancellationCode": "B",
                "DepDelay": None,
                "ArrDelay": None,
            },
            {"Flight_Number_Reporting_Airline": "200", "Diverted": "1.00", "ArrDelay": None},
        ),
        AIRPORTS,
    )
    assert result.rejected.num_rows == 0
    rows = {r["flight_number"]: r for r in result.valid.to_pylist()}
    assert rows[100]["status"] == "cancelled"
    assert rows[100]["cancellation_code"] == "B"
    assert rows[100]["arr_utc"] is None
    assert rows[200]["status"] == "diverted"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"Dest": "XXX"}, "unknown_dest_airport"),
        ({"Origin": "XXX"}, "unknown_origin_airport"),
        ({"CRSArrTime": "1200"}, "schedule_inconsistent"),
        ({"CRSDepTime": "8:00"}, "invalid_sched_dep_time"),
        ({"CRSDepTime": "0875"}, "invalid_sched_dep_time"),
        ({"FlightDate": "2026-13-01"}, "invalid_flight_date"),
        ({"Flight_Number_Reporting_Airline": "abc"}, "invalid_flight_number"),
        ({"ArrDelay": None}, "missing_arr_delay"),
        ({"Cancelled": "1.00", "Diverted": "1.00"}, "cancelled_and_diverted"),
        ({"DepDelay": "5000.00"}, "delay_out_of_range"),
    ],
)
def test_contract_violations_go_to_quarantine(override: dict[str, Any], reason: str) -> None:
    result = build_silver(bronze(override), AIRPORTS)
    assert result.valid.num_rows == 0
    (row,) = result.rejected.to_pylist()
    assert row["reject_reason"] == reason
    # Quarantine keeps the original text so the row can be inspected or replayed later.
    assert row["FlightDate"] == (BASE_ROW | override)["FlightDate"]


def test_every_row_lands_in_exactly_one_place() -> None:
    result = build_silver(
        bronze({}, {"Dest": "XXX"}, {"Flight_Number_Reporting_Airline": "7"}), AIRPORTS
    )
    assert result.valid.num_rows + result.rejected.num_rows == 3


def test_flight_ids_are_unique_and_stable() -> None:
    table = bronze({}, {"Flight_Number_Reporting_Airline": "200"})
    first = build_silver(table, AIRPORTS).valid.column("flight_id").to_pylist()
    second = build_silver(table, AIRPORTS).valid.column("flight_id").to_pylist()
    assert len(set(first)) == 2
    assert first == second


def test_output_is_identical_regardless_of_input_order() -> None:
    rows = [{"Flight_Number_Reporting_Airline": str(n)} for n in (3, 1, 2)]
    forward = build_silver(bronze(*rows), AIRPORTS)
    backward = build_silver(bronze(*reversed(rows)), AIRPORTS)
    assert forward.valid.equals(backward.valid)


def test_renamed_airport_codes_map_to_current_code() -> None:
    # Palm Beach was PBI, now DJT. Old and new flights must share one airport identity.
    old = only_valid_row(Dest="PBI", CRSArrTime="1100", CRSElapsedTime="180.00")
    new = only_valid_row(Dest="DJT", CRSArrTime="1100", CRSElapsedTime="180.00")
    assert old["dest"] == new["dest"] == "DJT"
    assert old["flight_id"] == new["flight_id"]


def test_same_flight_number_to_two_destinations_gets_two_ids() -> None:
    # A multi-stop flight number can be reported as JFK->LAX and JFK->HNL at the same time.
    to_lax = {}
    to_hnl = {"Dest": "HNL", "CRSArrTime": "1300", "CRSElapsedTime": "660.00"}
    ids = build_silver(bronze(to_lax, to_hnl), AIRPORTS).valid.column("flight_id").to_pylist()
    assert len(set(ids)) == 2
