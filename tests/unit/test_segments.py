from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa

from flightops.ingestion.opensky import SCHEMA
from flightops.tracking.segments import split_legs

START = int(datetime(2026, 11, 9, 12, 0, tzinfo=UTC).timestamp())

# Test planes fly east along the equator at a steady 220 m/s, so positions fit their speed.
SPEED_MS = 220.0
DEGREES_PER_MINUTE = SPEED_MS * 60 / 111_195  # metres per degree of longitude at the equator


def at(minute: int) -> datetime:
    return datetime.fromtimestamp(START, UTC) + timedelta(minutes=minute)


def track(
    icao24: str, *rows: tuple[Any, ...], callsign: str = "DAL1184 ", minutes_behind: int = 0
) -> list[dict[str, Any]]:
    """Rows of (minute, state[, altitude]) for one plane, one per 5-minute snapshot.

    state is "G" (on the ground) or "A" (airborne). minutes_behind places the plane that far
    back along its path, as if it had stopped somewhere for that long.
    """
    out = []
    for minute, state, *altitude in rows:
        on_ground = state == "G"
        out.append(
            {
                "snapshot_time": START + minute * 60,
                "icao24": icao24,
                "callsign": callsign,
                "time_position": START + minute * 60,
                "latitude": 0.0,
                "longitude": -84.4 + (minute - minutes_behind) * DEGREES_PER_MINUTE,
                "on_ground": on_ground,
                "baro_altitude": None if on_ground else (altitude[0] if altitude else 10000.0),
                "velocity": 5.0 if on_ground else SPEED_MS,
                "vertical_rate": None if on_ground else 0.0,
            }
        )
    return out


def snapshots(*tracks: list[dict[str, Any]]) -> pa.Table:
    rows = [row for t in tracks for row in t]
    return pa.Table.from_pylist(rows, schema=SCHEMA)


def every(start: int, end: int, state: str) -> list[tuple[int, str]]:
    return [(m, state) for m in range(start, end + 1, 5)]


def legs(*tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return split_legs(snapshots(*tracks)).to_pylist()


def test_clean_flight_is_one_leg_with_takeoff_and_landing() -> None:
    (leg,) = legs(track("a1b2c3", *every(0, 5, "G"), *every(10, 60, "A"), *every(65, 90, "G")))
    assert leg["callsign"] == "DAL1184"
    assert (leg["first_air_time"], leg["last_air_time"]) == (at(10), at(60))
    assert (leg["start_reason"], leg["end_reason"]) == ("takeoff_seen", "landing_seen")
    assert (leg["last_ground_time"], leg["first_ground_time"]) == (at(5), at(65))
    assert leg["ground_blips"] == 0


def test_touch_and_go_around_stays_one_leg() -> None:
    # Wheels touch at minute 30 (caught by one snapshot), then the plane climbs away again.
    rows = [*every(0, 5, "G"), *every(10, 25, "A"), (30, "G"), *every(35, 60, "A")]
    (leg,) = legs(track("a1b2c3", *rows, *every(65, 90, "G")))
    assert (leg["first_air_time"], leg["last_air_time"]) == (at(10), at(60))
    assert leg["ground_blips"] == 1


def test_go_around_stays_one_leg() -> None:
    approach = [(40, "A", 300.0), (45, "A", 900.0), (50, "A", 1200.0), (55, "A", 300.0)]
    (leg,) = legs(track("a1b2c3", *every(0, 5, "G"), *every(10, 35, "A"), *approach, (60, "G")))
    assert (leg["last_air_time"], leg["first_ground_time"]) == (at(55), at(60))
    assert leg["end_reason"] == "landing_seen"


def test_one_airborne_blip_while_parked_is_not_a_flight() -> None:
    assert legs(track("a1b2c3", *every(0, 20, "G"), (25, "A"), *every(30, 60, "G"))) == []


def test_parked_all_day_has_no_legs() -> None:
    assert legs(track("a1b2c3", *every(0, 120, "G"))) == []


def test_two_flights_with_a_turnaround() -> None:
    first, second = legs(
        track(
            "a1b2c3",
            *every(0, 5, "G"),
            *every(10, 60, "A"),
            *every(65, 105, "G"),
            *every(110, 180, "A"),
            *every(185, 200, "G"),
        )
    )
    assert (first["first_air_time"], first["last_air_time"]) == (at(10), at(60))
    assert (second["first_air_time"], second["last_air_time"]) == (at(110), at(180))
    # Same callsign on both legs (like DAL2679 on Nov 9), but the takeoff was seen: two flights.
    assert second["start_reason"] == "takeoff_seen"
    assert first["coverage_gaps"] == second["coverage_gaps"] == 0


def test_vanishing_and_reappearing_hours_later_is_two_legs() -> None:
    # Like DAL3083 on Nov 9: lost while descending, seen again flying 13 hours later.
    descent = [(55, "A", 3000.0), (60, "A", 2000.0)]
    first, second = legs(
        track("a1b2c3", *every(0, 5, "G"), *every(10, 50, "A"), *descent, *every(830, 870, "A"))
    )
    assert (first["start_reason"], first["end_reason"]) == ("takeoff_seen", "vanished")
    assert second["start_reason"] == "appeared_airborne"
    assert first["first_ground_time"] is None


def test_coverage_hole_at_cruise_is_joined_back_into_one_leg() -> None:
    # Out of sight from minute 60 to 150, still flying at the same speed: an ocean crossing.
    rows = [*every(0, 5, "G"), *every(10, 60, "A"), *every(150, 200, "A"), *every(205, 220, "G")]
    (leg,) = legs(track("a1b2c3", *rows))
    assert (leg["first_air_time"], leg["last_air_time"]) == (at(10), at(200))
    assert (leg["start_reason"], leg["end_reason"]) == ("takeoff_seen", "landing_seen")
    assert (leg["last_ground_time"], leg["first_ground_time"]) == (at(5), at(205))
    assert leg["coverage_gaps"] == 1
    assert leg["positions"] == 22


def test_several_coverage_holes_are_all_joined() -> None:
    rows = [*every(10, 60, "A"), *every(120, 150, "A"), *every(210, 240, "A")]
    (leg,) = legs(track("a1b2c3", *rows))
    assert leg["coverage_gaps"] == 2


def test_lost_low_is_not_joined() -> None:
    # Last seen at 1,500 m: it may have landed in the gap.
    first = track("a1b2c3", *every(10, 55, "A"), (60, "A", 1500.0))
    assert len(legs(first, track("a1b2c3", *every(150, 200, "A")))) == 2


def test_different_callsign_is_not_joined() -> None:
    first = track("a1b2c3", *every(10, 60, "A"))
    second = track("a1b2c3", *every(150, 200, "A"), callsign="DAL2446 ")
    assert len(legs(first, second)) == 2


def test_too_little_distance_for_the_gap_is_not_joined() -> None:
    # Reappears only 40 minutes further along after a 90-minute gap: it stopped somewhere.
    first = track("a1b2c3", *every(10, 60, "A"))
    second = track("a1b2c3", *every(150, 200, "A"), minutes_behind=50)
    assert len(legs(first, second)) == 2


def test_one_missed_snapshot_does_not_split_a_flight() -> None:
    rows = [*every(10, 30, "A"), *every(40, 60, "A")]  # minute 35 missing: a 10-minute gap
    assert len(legs(track("a1b2c3", *rows))) == 1


def test_flight_still_in_the_air_at_the_end_of_the_data() -> None:
    (leg,) = legs(track("a1b2c3", *every(0, 5, "G"), *every(10, 60, "A")))
    assert leg["end_reason"] == "still_flying"


def test_vanished_is_judged_against_the_whole_snapshot_not_the_plane() -> None:
    # This plane stops at minute 60, but the data runs to minute 120: it vanished.
    lost = track("a1b2c3", *every(10, 60, "A"))
    other = track("d4e5f6", *every(0, 120, "G"))
    (leg,) = legs(lost, other)
    assert leg["end_reason"] == "vanished"


def test_stale_repeats_of_the_last_position_are_ignored() -> None:
    rows = track("a1b2c3", *every(10, 60, "A"), *every(65, 90, "G"))
    flying = [r for r in rows if not r["on_ground"]]
    # OpenSky keeps listing the plane at its last position for one more snapshot.
    stale = {**flying[-1], "snapshot_time": flying[-1]["snapshot_time"] + 300}
    (leg,) = legs([*flying, stale])
    assert leg["last_air_time"] == at(60)
    assert leg["positions"] == len(flying)


def test_most_common_callsign_wins_and_blank_is_ignored() -> None:
    main = track("a1b2c3", *every(10, 40, "A"), callsign="DAL1184 ")
    other = track("a1b2c3", (45, "A"), callsign="DAL9")
    blank = track("a1b2c3", (50, "A"), callsign="        ")
    (leg,) = legs(main, other, blank)
    assert leg["callsign"] == "DAL1184"


def test_output_is_identical_regardless_of_input_order() -> None:
    table = snapshots(
        track("a1b2c3", *every(0, 5, "G"), *every(10, 60, "A"), *every(65, 90, "G")),
        track("d4e5f6", *every(0, 30, "A"), *every(35, 90, "G")),
    )
    reversed_rows = table.take(list(range(table.num_rows - 1, -1, -1)))
    assert split_legs(table).equals(split_legs(reversed_rows))
