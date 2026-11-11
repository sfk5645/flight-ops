"""adsb.lol daily history -> positions shaped like our OpenSky snapshots.

adsb.lol publishes one archive per UTC day as a GitHub release (github.com/adsblol/
globe_history_<year>, Open Database License: credit adsb.lol when publishing results), split
into ~2 GB parts of one tar file. Inside, traces/xx/trace_full_<icao>.json is one gzipped JSON
per aircraft with a point every few seconds:

    [seconds after "timestamp", lat, lon, altitude ft or "ground", ground speed kt, track,
     flags, vertical rate ft/min, details or null, ...]

The callsign is only in `details`, and only when it changes, so it is carried forward.

The archive (~4 GB a day) is read as it downloads and never written to disk.

The live pipeline sees one position per plane every 5 minutes, so the trace is thinned to the
same: for each 5-minute snapshot time T, the latest point in (T - 5 min, T]. Testing tracking on
the full-rate trace would grade a better feed than the one we actually run on.
"""

import gzip
import io
import itertools
import json
import tarfile
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import date
from typing import IO, Any

import httpx
import pyarrow as pa

from flightops.ingestion.download import USER_AGENT
from flightops.tracking.segments import POSITION_COLUMNS

RELEASE_URL = (
    "https://api.github.com/repos/adsblol/globe_history_{year}/releases/tags/"
    "v{day:%Y.%m.%d}-planes-readsb-prod-0"
)

SNAPSHOT_EVERY_S = 300

FEET = 0.3048
KNOTS = 0.514444
FEET_PER_MIN = FEET / 60

# flags bit 0: the position is stale (repeated from an earlier message), not a new fix.
STALE_FLAG = 1

# Traces handed to each worker process at a time.
BATCH = 500

POSITIONS_SCHEMA = pa.schema(
    [
        ("snapshot_time", pa.int64()),
        ("icao24", pa.string()),
        ("callsign", pa.string()),
        ("time_position", pa.int64()),
        ("latitude", pa.float64()),
        ("longitude", pa.float64()),
        ("on_ground", pa.bool_()),
        ("baro_altitude", pa.float64()),
        ("geo_altitude", pa.float64()),
        ("velocity", pa.float64()),
        ("true_track", pa.float64()),
        ("vertical_rate", pa.float64()),
    ]
)
assert POSITIONS_SCHEMA.names == POSITION_COLUMNS


def _scaled(value: Any, factor: float) -> float | None:
    return value * factor if isinstance(value, int | float) else None


def snapshot_rows(trace: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """One row per 5-minute snapshot the aircraft appears in, in OpenSky units."""
    icao24 = trace["icao"]
    if icao24.startswith("~"):  # not a real ICAO address (TIS-B / ground radar targets)
        return
    start = trace["timestamp"]
    callsign = None
    pending: dict[str, Any] | None = None
    for point in trace["trace"]:
        offset, lat, lon, altitude, speed, track, flags, rate = point[:8]
        details = point[8] if len(point) > 8 else None
        if isinstance(details, dict) and details.get("flight", "").strip():
            callsign = details["flight"].strip()
        if flags & STALE_FLAG or lat is None or lon is None:
            continue
        t = start + offset
        snapshot = -(-int(t) // SNAPSHOT_EVERY_S) * SNAPSHOT_EVERY_S  # next multiple of 5 min
        if pending is not None and pending["snapshot_time"] != snapshot:
            yield pending
        on_ground = altitude == "ground"
        pending = {
            "snapshot_time": snapshot,
            "icao24": icao24,
            "callsign": callsign,
            "time_position": int(t),
            "latitude": lat,
            "longitude": lon,
            "on_ground": on_ground,
            "baro_altitude": None if on_ground else _scaled(altitude, FEET),
            "geo_altitude": _scaled(point[10] if len(point) > 10 else None, FEET),
            "velocity": _scaled(speed, KNOTS),
            "true_track": track,
            "vertical_rate": _scaled(rate, FEET_PER_MIN),
        }
    if pending is not None:
        yield pending


def _positions(traces: list[bytes]) -> pa.Table:
    """Gzipped trace files -> positions (runs in a worker process)."""
    rows = [row for trace in traces for row in snapshot_rows(json.loads(gzip.decompress(trace)))]
    return pa.Table.from_pylist(rows, schema=POSITIONS_SCHEMA)


def trace_files(archive: IO[bytes]) -> Iterator[bytes]:
    """The gzipped traces/ files of a day's tar archive, read front to back."""
    with tarfile.open(fileobj=archive, mode="r|") as tar:
        for member in tar:
            if member.isfile() and "/traces/" in f"/{member.name.removeprefix('./')}":
                file = tar.extractfile(member)
                assert file is not None
                yield file.read()


def read_traces(traces: Iterable[bytes], *, workers: int = 8) -> pa.Table:
    """Positions from gzipped trace files, sorted by snapshot time.

    Parsing is spread over worker processes. Only a few batches are in flight at once, so a
    whole day is never held in memory as raw files.
    """
    tables: list[pa.Table] = []
    pending: list[Future[pa.Table]] = []
    with ProcessPoolExecutor(workers) as pool:
        for batch in itertools.batched(traces, BATCH):
            pending.append(pool.submit(_positions, list(batch)))
            if len(pending) >= 2 * workers:
                tables.append(pending.pop(0).result())
        tables += [future.result() for future in pending]
    return pa.concat_tables(tables).sort_by([("snapshot_time", "ascending")])


class _ChunkReader(io.RawIOBase):
    """A readable file over an iterator of byte chunks."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        while not self._buffer:
            self._buffer = next(self._chunks, b"")
            if not self._buffer:
                return 0
        n = min(len(target), len(self._buffer))
        target[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return n


def archive_urls(day: date) -> list[str]:
    """Download links of the day's archive parts, in order (.tar.aa, .tar.ab, ...)."""
    response = httpx.get(
        RELEASE_URL.format(year=day.year, day=day),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=60,
    )
    response.raise_for_status()
    return sorted(asset["browser_download_url"] for asset in response.json()["assets"])


def _download_chunks(urls: list[str]) -> Iterator[bytes]:
    for url in urls:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=300,
        ) as response:
            response.raise_for_status()
            yield from response.iter_bytes(chunk_size=1 << 20)


def read_day(day: date, *, workers: int = 8) -> pa.Table:
    """Snapshot-shaped positions for every aircraft adsb.lol saw on `day` (UTC)."""
    archive = io.BufferedReader(_ChunkReader(_download_chunks(archive_urls(day))), 1 << 20)
    return read_traces(trace_files(archive), workers=workers)
