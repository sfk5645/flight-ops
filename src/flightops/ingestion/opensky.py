"""OpenSky live positions → bronze.

Each call to ingest_snapshot() asks OpenSky for every aircraft's current state (one global
/states/all request, 4 API credits) and stores the snapshot as one Parquet file:

    bronze/opensky_states/date=2026-11-06/1762450200.parquet

The R2 lifecycle rule deletes these after 3 days (see docs/setup.md). Rows that break the
contract go to quarantine/opensky_states/ with a reason.

Data from the OpenSky Network (https://opensky-network.org). Use is subject to its terms:
non-profit research and education, no redistribution, and no aircraft identifiers in anything
published.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pyarrow as pa
from pydantic import ValidationError

from flightops.config import OpenSkySettings
from flightops.contracts.opensky import StatesResponse, StateVector
from flightops.ingestion.download import USER_AGENT
from flightops.storage import LakeStorage, Layer, lake_key

log = logging.getLogger(__name__)

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
STATES_URL = "https://opensky-network.org/api/states/all"
DATASET = "opensky_states"

# Refresh the 30-minute access token a minute early, so it can't expire mid-request.
TOKEN_EARLY_REFRESH_S = 60

SCHEMA = pa.schema(
    [
        ("snapshot_time", pa.int64()),
        ("icao24", pa.string()),
        ("callsign", pa.string()),
        ("origin_country", pa.string()),
        ("time_position", pa.int64()),
        ("last_contact", pa.int64()),
        ("longitude", pa.float64()),
        ("latitude", pa.float64()),
        ("baro_altitude", pa.float64()),
        ("on_ground", pa.bool_()),
        ("velocity", pa.float64()),
        ("true_track", pa.float64()),
        ("vertical_rate", pa.float64()),
        ("sensors", pa.list_(pa.int32())),
        ("geo_altitude", pa.float64()),
        ("squawk", pa.string()),
        ("spi", pa.bool_()),
        ("position_source", pa.int8()),
        ("category", pa.int8()),
    ]
)

REJECTED_SCHEMA = pa.schema(
    [("snapshot_time", pa.int64()), ("reject_reason", pa.string()), ("raw_row", pa.string())]
)


class OpenSkyRateLimitError(Exception):
    """Daily API credits are used up (HTTP 429)."""


class OpenSkyClient:
    """Minimal OpenSky REST client using OAuth2 client credentials."""

    def __init__(
        self,
        settings: OpenSkySettings,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _access_token(self, *, refresh: bool = False) -> str:
        if refresh or self._token is None or self._clock() >= self._token_expires_at:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret.get_secret_value(),
                },
            )
            response.raise_for_status()
            body = response.json()
            self._token = str(body["access_token"])
            self._token_expires_at = (
                self._clock() + float(body["expires_in"]) - TOKEN_EARLY_REFRESH_S
            )
        assert self._token is not None
        return self._token

    def get_states(self) -> StatesResponse:
        """All aircraft states right now (global, extended=1 adds the aircraft category)."""
        response = self._get(STATES_URL, params={"extended": 1})
        remaining = response.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None:
            log.info("opensky credits remaining today: %s", remaining)
        return StatesResponse.model_validate(response.json())

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        for refresh in (False, True):  # an expired token gets one retry with a fresh one
            headers = {"Authorization": f"Bearer {self._access_token(refresh=refresh)}"}
            response = self._http.get(url, params=params, headers=headers)
            if response.status_code != 401:
                break
        if response.status_code == 429:
            retry_after = response.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            raise OpenSkyRateLimitError(f"out of API credits; retry after {retry_after}s")
        response.raise_for_status()
        return response


@dataclass(frozen=True)
class Snapshot:
    time: int
    valid: pa.Table
    rejected: pa.Table


def parse_states(response: StatesResponse) -> Snapshot:
    """Check every state vector against the contract; sorted output, so reruns are identical."""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in response.states or []:
        try:
            state = StateVector.from_row(row)
        except (ValidationError, ValueError) as error:
            reason = _reject_reason(error)
            raw = json.dumps(row)
            rejected.append(
                {"snapshot_time": response.time, "reject_reason": reason, "raw_row": raw}
            )
        else:
            valid.append({"snapshot_time": response.time, **state.model_dump()})
    valid.sort(key=lambda r: (r["icao24"], r["last_contact"]))
    rejected.sort(key=lambda r: (r["reject_reason"], r["raw_row"]))
    return Snapshot(
        time=response.time,
        valid=pa.Table.from_pylist(valid, schema=SCHEMA),
        rejected=pa.Table.from_pylist(rejected, schema=REJECTED_SCHEMA),
    )


def _reject_reason(error: ValidationError | ValueError) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return f"{field}: {first['type']}"
    return str(error)


def snapshot_key(snapshot_time: int, layer: Layer = "bronze") -> str:
    date = datetime.fromtimestamp(snapshot_time, UTC).strftime("%Y-%m-%d")
    return lake_key(layer, DATASET, f"{snapshot_time}.parquet", date=date)


def ingest_snapshot(lake: LakeStorage, client: OpenSkyClient) -> Snapshot | None:
    """Fetch one snapshot and store it. Returns None if this snapshot time is already stored."""
    snapshot = parse_states(client.get_states())
    key = snapshot_key(snapshot.time)
    if lake.exists(key):
        log.info("snapshot %s already stored, skipping", key)
        return None
    lake.write_parquet(key, snapshot.valid)
    # Quarantine is only written when something was rejected: one empty file per snapshot
    # would mean ~288 tiny files a day that never expire.
    if snapshot.rejected.num_rows:
        lake.write_parquet(snapshot_key(snapshot.time, "quarantine"), snapshot.rejected)
    log.info(
        "snapshot %s: %d aircraft, %d rejected",
        snapshot.time,
        snapshot.valid.num_rows,
        snapshot.rejected.num_rows,
    )
    return snapshot
