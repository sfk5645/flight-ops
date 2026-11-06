import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from flightops.config import OpenSkySettings
from flightops.contracts.opensky import StatesResponse, StateVector
from flightops.ingestion.opensky import (
    STATES_URL,
    TOKEN_URL,
    OpenSkyClient,
    OpenSkyRateLimitError,
    ingest_snapshot,
    parse_states,
    snapshot_key,
)
from flightops.storage import LakeStorage

SNAPSHOT_TIME = 1762450200  # 2025-11-06 17:30:00 UTC

# A real-looking airborne state vector (extended=1, so 18 fields).
ROW: list[Any] = [
    "a1b2c3", "DAL1184 ", "United States", 1762450195, 1762450199,
    -84.43, 33.64, 10668.0, False, 230.5, 45.0, 0.0,
    None, 10900.0, "1234", False, 0, 4,
]  # fmt: skip


def row(**override: Any) -> list[Any]:
    fields = dict(zip(StateVector.model_fields, ROW, strict=True))
    return list({**fields, **override}.values())


def settings() -> OpenSkySettings:
    return OpenSkySettings(client_id="test-client", client_secret=SecretStr("test-secret"))


class FakeOpenSky:
    """Stands in for OpenSky's auth server and API; records what the client did."""

    def __init__(self, states: list[list[Any]], states_status: list[int] | None = None) -> None:
        self.states = states
        self.states_status = list(states_status or [])
        self.tokens_issued = 0
        self.state_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens_issued}", "expires_in": 1800}
            )
        assert str(request.url).startswith(STATES_URL)
        assert request.headers["Authorization"] == f"Bearer token-{self.tokens_issued}"
        self.state_requests += 1
        status = self.states_status.pop(0) if self.states_status else 200
        headers = {"X-Rate-Limit-Retry-After-Seconds": "3600"} if status == 429 else {}
        return httpx.Response(
            status, headers=headers, json={"time": SNAPSHOT_TIME, "states": self.states}
        )

    def client(self, clock: list[float] | None = None) -> OpenSkyClient:
        now = clock if clock is not None else [0.0]
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return OpenSkyClient(settings(), http=http, clock=lambda: now[0])


def test_valid_row_matches_contract() -> None:
    state = StateVector.from_row(ROW)
    assert state.icao24 == "a1b2c3"
    assert state.callsign == "DAL1184 "  # stored as sent; trimming happens in silver
    assert state.category == 4


def test_row_without_category_is_accepted() -> None:
    assert StateVector.from_row(ROW[:17]).category is None


@pytest.mark.parametrize(
    ("bad_row", "reason"),
    [
        (row(icao24="XYZ"), "icao24: string_pattern_mismatch"),
        (row(latitude=95.0), "latitude: less_than_equal"),
        (row(on_ground="maybe"), "on_ground: bool_parsing"),
        (ROW[:5], "expected 17 or 18 fields, got 5"),
    ],
)
def test_bad_rows_are_quarantined_with_a_reason(bad_row: list[Any], reason: str) -> None:
    snapshot = parse_states(StatesResponse(time=SNAPSHOT_TIME, states=[ROW, bad_row]))
    assert snapshot.valid.num_rows == 1
    assert snapshot.rejected.column("reject_reason").to_pylist() == [reason]
    assert json.loads(snapshot.rejected.column("raw_row")[0].as_py()) == bad_row


def test_empty_response_gives_empty_tables() -> None:
    snapshot = parse_states(StatesResponse(time=SNAPSHOT_TIME, states=None))
    assert snapshot.valid.num_rows == snapshot.rejected.num_rows == 0


def test_output_is_identical_regardless_of_input_order() -> None:
    rows = [row(icao24="aaaaaa"), row(icao24="cccccc"), row(icao24="bbbbbb")]
    forward = parse_states(StatesResponse(time=SNAPSHOT_TIME, states=rows))
    backward = parse_states(StatesResponse(time=SNAPSHOT_TIME, states=rows[::-1]))
    assert forward.valid.equals(backward.valid)


def test_token_is_reused_until_it_nearly_expires() -> None:
    fake, clock = FakeOpenSky([ROW]), [0.0]
    client = fake.client(clock)
    client.get_states()
    clock[0] = 1700.0  # 28 min later: still valid
    client.get_states()
    assert fake.tokens_issued == 1
    clock[0] = 1750.0  # within a minute of the 30-minute expiry: refresh first
    client.get_states()
    assert fake.tokens_issued == 2


def test_rejected_token_is_refreshed_once() -> None:
    fake = FakeOpenSky([ROW], states_status=[401])
    fake.client().get_states()
    assert fake.tokens_issued == 2
    assert fake.state_requests == 2


def test_out_of_credits_raises() -> None:
    fake = FakeOpenSky([ROW], states_status=[429])
    with pytest.raises(OpenSkyRateLimitError, match="retry after 3600s"):
        fake.client().get_states()


def test_ingest_writes_bronze_and_skips_a_repeat(lake: LakeStorage) -> None:
    fake = FakeOpenSky([ROW, row(icao24="bad")])
    snapshot = ingest_snapshot(lake, fake.client())
    assert snapshot is not None
    key = snapshot_key(SNAPSHOT_TIME)
    assert key == "bronze/opensky_states/date=2025-11-06/1762450200.parquet"
    assert lake.read_parquet(key).num_rows == 1
    assert lake.exists(snapshot_key(SNAPSHOT_TIME, "quarantine"))
    assert ingest_snapshot(lake, fake.client()) is None


def test_no_quarantine_file_when_nothing_is_rejected(lake: LakeStorage) -> None:
    ingest_snapshot(lake, FakeOpenSky([ROW]).client())
    assert not lake.exists(snapshot_key(SNAPSHOT_TIME, "quarantine"))
