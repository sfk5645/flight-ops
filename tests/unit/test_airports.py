import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest

from flightops.tracking.airports import match_airports

AIRPORTS = pa.table(
    {
        "iata": ["ATL", "LGA", "JFK", "DEN", "XHP", "SMA"],
        "type": ["large_airport"] * 4 + ["heliport", "small_airport"],
        # SMA: a small airfield 6 km north of ATL, like Hawthorne next to LAX.
        "latitude": [33.6367, 40.7772, 40.6398, 39.8617, 40.7700, 33.6907],
        "longitude": [-84.4281, -73.8726, -73.7789, -104.6731, -73.8700, -84.4281],
        "elevation_ft": [1026, 21, 13, 5431, 7, 1000],
    }
)
LGA = (40.7772, -73.8726)
ATL = (33.6367, -84.4281)
DEN = (39.8617, -104.6731)

TAKEOFF = datetime(2026, 11, 9, 16, 50, tzinfo=UTC)
LAST_AIR = datetime(2026, 11, 9, 18, 50, tzinfo=UTC)

# A seen ATL -> LGA flight: last ground row on an ATL taxiway, first ground row at LGA.
BASE_LEG: dict[str, Any] = {
    "icao24": "a1b2c3",
    "start_reason": "takeoff_seen",
    "end_reason": "landing_seen",
    "last_ground_time": TAKEOFF - timedelta(minutes=5),
    "last_ground_latitude": 33.6400,
    "last_ground_longitude": -84.4400,
    "first_air_time": TAKEOFF,
    "first_latitude": 33.66,
    "first_longitude": -84.40,
    "first_altitude_m": 313.0 + 600,  # 600 m above ATL (elevation 313 m)
    "first_vertical_rate_ms": 10.0,
    "first_speed_ms": 80.0,
    "first_track_deg": 90.0,
    "last_air_time": LAST_AIR,
    "last_latitude": 40.75,
    "last_longitude": -73.90,
    "last_altitude_m": 6.4 + 300,  # 300 m above LGA (elevation 6.4 m)
    "last_vertical_rate_ms": -5.0,
    "last_speed_ms": 75.0,
    "last_track_deg": 45.0,
    "first_ground_time": LAST_AIR + timedelta(minutes=5),
    "first_ground_latitude": 40.7750,
    "first_ground_longitude": -73.8800,
}

# A leg whose takeoff and landing were never seen on the ground.
UNSEEN: dict[str, Any] = {
    "start_reason": "appeared_airborne",
    "end_reason": "vanished",
    "last_ground_time": None,
    "last_ground_latitude": None,
    "last_ground_longitude": None,
    "first_ground_time": None,
    "first_ground_latitude": None,
    "first_ground_longitude": None,
}


def behind(point: tuple[float, float], metres: float, heading_deg: float) -> tuple[float, float]:
    """The point `metres` before reaching `point` when flying on `heading_deg`."""
    lat, lon = point
    dlat = -metres * math.cos(math.radians(heading_deg)) / 111_195
    dlon = -metres * math.sin(math.radians(heading_deg)) / (111_195 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def on_approach(
    airport: tuple[float, float], height_m: float, elevation_m: float, heading: float = 45.0
) -> dict[str, Any]:
    """Lost while descending at 4 m/s and 75 m/s, aimed straight at `airport`."""
    seconds = height_m / 4.0
    lat, lon = behind(airport, 75.0 * seconds, heading)
    return UNSEEN | {
        "last_latitude": lat,
        "last_longitude": lon,
        "last_altitude_m": elevation_m + height_m,
        "last_vertical_rate_ms": -4.0,
        "last_speed_ms": 75.0,
        "last_track_deg": heading,
    }


def about(actual: datetime, expected: datetime) -> bool:
    """Within a second: airport elevations are in whole feet, so heights aren't round."""
    return abs(actual - expected) < timedelta(seconds=1)


def matched(*overrides: dict[str, Any]) -> list[dict[str, Any]]:
    legs = pa.Table.from_pylist([BASE_LEG | o for o in overrides])
    return match_airports(legs, AIRPORTS).to_pylist()


def test_seen_takeoff_and_landing_get_the_nearest_airport() -> None:
    (leg,) = matched({})
    assert (leg["origin"], leg["origin_how"]) == ("ATL", "seen")
    assert (leg["dest"], leg["dest_how"]) == ("LGA", "seen")


def test_seen_times_come_from_height_and_rate() -> None:
    # 600 m above ATL climbing 10 m/s: lifted off 60 s earlier. 300 m above LGA descending
    # 5 m/s: touches down 60 s later.
    (leg,) = matched({})
    assert about(leg["takeoff_time"], TAKEOFF - timedelta(seconds=60))
    assert about(leg["landing_time"], LAST_AIR + timedelta(seconds=60))


def test_seen_times_stay_inside_the_window() -> None:
    # Barely descending: the estimate would be 10 min, but it was on the ground 5 min later.
    (leg,) = matched({"last_vertical_rate_ms": -0.1, "last_altitude_m": 1500.0})
    assert leg["landing_time"] == LAST_AIR + timedelta(minutes=5)


def test_neighbouring_airports_are_told_apart() -> None:
    # JFK is 17 km from LGA; a plane on the ground at JFK must not be matched to LGA.
    (leg,) = matched({"first_ground_latitude": 40.6420, "first_ground_longitude": -73.7800})
    assert leg["dest"] == "JFK"


def test_heliports_are_ignored() -> None:
    # The heliport is nearer to this point than LGA is, but airliners don't land there.
    (leg,) = matched({"first_ground_latitude": 40.7705, "first_ground_longitude": -73.8705})
    assert leg["dest"] == "LGA"


def test_no_airport_within_reach_leaves_it_empty() -> None:
    (leg,) = matched({"first_ground_latitude": 38.0, "first_ground_longitude": -80.0})
    assert (leg["dest"], leg["dest_how"], leg["landing_time"]) == (None, None, None)


def test_landing_lost_on_approach_is_inferred() -> None:
    (leg,) = matched(on_approach(LGA, height_m=500, elevation_m=6.4))
    assert (leg["dest"], leg["dest_how"]) == ("LGA", "inferred")
    assert about(leg["landing_time"], LAST_AIR + timedelta(seconds=125))  # 500 m at 4 m/s


def test_height_is_measured_above_the_airport_not_sea_level() -> None:
    # 2,155 m above sea level is only 500 m above Denver.
    (leg,) = matched(on_approach(DEN, height_m=500, elevation_m=1655.4))
    assert (leg["dest"], leg["dest_how"]) == ("DEN", "inferred")


def test_flying_away_from_an_airport_is_not_a_landing() -> None:
    # Same spot as an approach to LGA, but heading the opposite way.
    approach = on_approach(LGA, height_m=500, elevation_m=6.4)
    (leg,) = matched(approach | {"last_track_deg": 225.0})
    assert leg["dest"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"last_altitude_m": 3000.0},  # still high
        {"last_vertical_rate_ms": 6.0, "last_altitude_m": 900.0},  # climbing away
    ],
)
def test_lost_high_or_climbing_is_not_a_landing(change: dict[str, Any]) -> None:
    (leg,) = matched(on_approach(LGA, height_m=500, elevation_m=6.4) | change)
    assert leg["dest"] is None


def test_takeoff_seen_first_in_the_air_is_inferred() -> None:
    # First seen 600 m above ATL, climbing 10 m/s at 80 m/s heading east: lifted off 60 s
    # (4.8 km) earlier, back at the airport.
    (leg,) = matched(climbing_out_of(ATL))
    assert (leg["origin"], leg["origin_how"]) == ("ATL", "inferred")
    assert about(leg["takeoff_time"], TAKEOFF - timedelta(seconds=60))


def climbing_out_of(point: tuple[float, float]) -> dict[str, Any]:
    """First seen 600 m up, climbing 10 m/s at 80 m/s heading east, 60 s after leaving `point`."""
    lat, lon = behind(point, -4800.0, 90.0)
    return UNSEEN | {"first_latitude": lat, "first_longitude": lon}


def test_takeoff_between_two_close_airports_is_ambiguous() -> None:
    (leg,) = matched(climbing_out_of((33.6637, -84.4281)))  # halfway between ATL and SMA
    assert leg["origin_how"] == "inferred_ambiguous"


def later_leg(**change: Any) -> dict[str, Any]:
    return {"first_air_time": LAST_AIR + timedelta(hours=1), "last_air_time": None} | change


def test_takeoff_is_where_the_plane_last_landed() -> None:
    # The second leg's projection points at SMA, but the plane landed at ATL last time.
    first = BASE_LEG | {"first_ground_latitude": 33.6400, "first_ground_longitude": -84.4300}
    second = later_leg(**climbing_out_of((33.6907, -84.4281)))
    legs = match_airports(pa.Table.from_pylist([first, BASE_LEG | second]), AIRPORTS).to_pylist()
    assert (legs[0]["dest"], legs[0]["dest_how"]) == ("ATL", "seen")
    assert (legs[1]["origin"], legs[1]["origin_how"]) == ("ATL", "previous_leg")


def test_first_seen_at_cruise_takes_off_from_where_it_last_landed() -> None:
    # Nothing to project back from (already at 10 km), but the previous leg landed at LGA.
    second = later_leg(**UNSEEN, first_altitude_m=10000.0, first_vertical_rate_ms=0.0)
    legs = matched({}, second)
    assert (legs[1]["origin"], legs[1]["origin_how"]) == ("LGA", "previous_leg")
    assert legs[1]["takeoff_time"] is None


def test_seen_takeoff_beats_the_previous_leg() -> None:
    # Landed at LGA, next seen taking off from ATL: trust what was seen.
    legs = matched({}, later_leg())
    assert (legs[1]["origin"], legs[1]["origin_how"]) == ("ATL", "seen")


def test_previous_leg_without_a_destination_falls_back_to_inferred() -> None:
    lost_at_cruise = UNSEEN | {"last_altitude_m": 10000.0}
    second = later_leg(**climbing_out_of(ATL))
    legs = matched(lost_at_cruise, second)
    assert legs[0]["dest"] is None
    assert (legs[1]["origin"], legs[1]["origin_how"]) == ("ATL", "inferred")


def test_one_row_per_leg_in_order() -> None:
    later = {"icao24": "000001"}
    legs = matched({}, later)
    assert [leg["icao24"] for leg in legs] == ["000001", "a1b2c3"]
