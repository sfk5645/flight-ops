import gzip
import io
import json
import tarfile
from typing import Any

import pytest

from flightops.ingestion.adsblol import (
    POSITIONS_SCHEMA,
    _ChunkReader,
    read_traces,
    snapshot_rows,
    trace_files,
)

START = 1_787_011_200  # 2026-08-18 00:00 UTC


def point(
    offset: float,
    altitude: float | str = 35000,
    callsign: str | None = None,
    flags: int = 0,
) -> list[Any]:
    details = {"flight": f"{callsign:<8}"} if callsign else None
    return [offset, 33.64, -84.43, altitude, 450.0, 90.0, flags, -1000, details, "adsb_icao", 35100]


def rows(*points: list[Any], icao: str = "ab3c61") -> list[dict[str, Any]]:
    return list(snapshot_rows({"icao": icao, "timestamp": START, "trace": list(points)}))


def test_converts_units_to_opensky() -> None:
    (row,) = rows(point(10, callsign="DAL927"))
    assert row["callsign"] == "DAL927"
    assert row["baro_altitude"] == pytest.approx(10668)  # 35,000 ft
    assert row["geo_altitude"] == pytest.approx(10698.48)
    assert row["velocity"] == pytest.approx(231.5)  # 450 kt in m/s
    assert row["vertical_rate"] == pytest.approx(-5.08)  # -1000 ft/min
    assert row["on_ground"] is False


def test_ground_points_have_no_altitude() -> None:
    (row,) = rows(point(10, altitude="ground"))
    assert (row["on_ground"], row["baro_altitude"]) == (True, None)


def test_keeps_the_latest_point_in_each_five_minutes() -> None:
    # Snapshot at 300 s covers (0, 300]; at 600 s covers (300, 600].
    out = rows(point(10), point(250), point(300), point(301), point(590))
    assert [(r["snapshot_time"] - START, r["time_position"] - START) for r in out] == [
        (300, 300),
        (600, 590),
    ]


def test_callsign_is_carried_forward_until_it_changes() -> None:
    out = rows(point(10, callsign="DAL927"), point(400), point(700, callsign="DAL659"))
    assert [r["callsign"] for r in out] == ["DAL927", "DAL927", "DAL659"]


def test_skips_stale_points_and_non_icao_addresses() -> None:
    assert [r["time_position"] - START for r in rows(point(10), point(20, flags=1))] == [10]
    assert rows(point(10), icao="~2b5f70") == []


def test_reads_traces_from_a_streamed_archive() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        files = {
            "./README.txt": b"not a trace",
            "./traces/61/trace_full_ab3c61.json": trace_file("ab3c61", 10),
            "./traces/c3/trace_full_a1b2c3.json": trace_file("a1b2c3", 400),
        }
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    chunks = iter([data[i : i + 1000] for i in range(0, len(data), 1000)])  # like a download
    archive = io.BufferedReader(_ChunkReader(chunks))

    table = read_traces(trace_files(archive), workers=1)

    assert table.schema == POSITIONS_SCHEMA
    assert table.column("icao24").to_pylist() == ["ab3c61", "a1b2c3"]  # snapshot order


def trace_file(icao: str, offset: float) -> bytes:
    trace = {"icao": icao, "timestamp": START, "trace": [point(offset)]}
    return gzip.compress(json.dumps(trace).encode())
