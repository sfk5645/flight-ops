"""Contract for the OpenSky /states/all response.

The API returns each aircraft as an unlabeled JSON array (a "state vector"). STATE_FIELDS names
the positions, and StateVector checks types and ranges, so a format change or a corrupt row is
caught at ingestion instead of being stored as if it were fine.

Reference: https://openskynetwork.github.io/opensky-api/rest.html#all-state-vectors
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Array positions, in the order the API sends them. "category" is only sent with extended=1.
STATE_FIELDS = (
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "longitude",
    "latitude",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
    "category",
)


class StateVector(BaseModel):
    """One aircraft at snapshot time. Units: metres, metres/second, degrees, Unix seconds."""

    model_config = ConfigDict(frozen=True)

    icao24: str = Field(pattern=r"^[0-9a-f]{6}$")  # transponder address, lowercase hex
    callsign: str | None  # e.g. "DAL1184 " (padded with spaces, as sent)
    origin_country: str
    time_position: int | None  # last position update; None if no position in the last 15 s
    last_contact: int
    longitude: float | None = Field(ge=-180, le=180)
    latitude: float | None = Field(ge=-90, le=90)
    baro_altitude: float | None
    on_ground: bool
    velocity: float | None = Field(ge=0)
    true_track: float | None  # heading over ground, degrees clockwise from north
    vertical_rate: float | None
    sensors: list[int] | None
    geo_altitude: float | None
    squawk: str | None
    spi: bool
    position_source: int = Field(ge=0, le=3)  # 0 ADS-B, 1 ASTERIX, 2 MLAT, 3 FLARM
    category: int | None = Field(default=None, ge=0, le=20)

    @classmethod
    def from_row(cls, row: list[Any]) -> StateVector:
        if len(row) not in (len(STATE_FIELDS) - 1, len(STATE_FIELDS)):
            raise ValueError(f"expected 17 or 18 fields, got {len(row)}")
        return cls.model_validate(dict(zip(STATE_FIELDS, row, strict=False)))


class StatesResponse(BaseModel):
    """The response envelope: the snapshot time and the list of raw state vectors."""

    time: int
    states: list[list[Any]] | None  # None when no aircraft match (e.g. an empty bounding box)
