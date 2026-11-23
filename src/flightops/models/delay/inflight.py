"""In-flight model: minutes from a moment during a flight until it lands (P10, P50, P90).

Inputs come from gold/inflight_training_set, where every column is known at the moment itself
(the dbt point-in-time test checks it). The destination is the expected one (the callsign's
usual destination), never where the plane actually landed. Trained with the same quantile
models and range calibration as the pre-departure model (models/delay/predep.py).

`feed` (opensky / adsblol) is deliberately not an input: live, every moment comes from OpenSky,
and positions are timed by when they were measured, so a moment means the same in both feeds.
Scores are still reported per feed, to see whether OpenSky moments come out worse.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from flightops.duck import connect_lake, lake_glob

LABEL = "minutes_to_landing"

FEATURE_GROUPS: dict[str, list[str]] = {
    "flight": ["carrier", "origin", "dest", "dest_hour_local", "dest_weekday_local"],
    "position": [
        "altitude_m", "speed_ms", "vertical_rate_ms", "dest_distance_km", "heading_off_deg",
        "direct_estimate_min",
    ],
    "progress": [
        "minutes_flown", "takeoff_known", "route_air_min", "route_flights_3m",
        "route_remaining_min", "expected_dest_flights",
    ],
}  # fmt: skip
FEATURES = [column for group in FEATURE_GROUPS.values() for column in group]
CATEGORICAL = ["carrier", "origin", "dest"]
# kept for slicing the scores, never inputs
EXTRA = ["feed", "date", "dest_guess_right"]

TEST_DAYS = 6  # newest days: the final score
VALID_DAYS = 5  # the days before: early stopping and range calibration

# Minutes-to-landing bands for reporting: the last half hour is where < 5 minutes is expected.
BANDS = [0, 15, 30, 60, 120, 240, np.inf]


@dataclass(frozen=True)
class Split:
    train: pd.DataFrame
    valid: pd.DataFrame
    test: pd.DataFrame


def load_training_set() -> pd.DataFrame:
    con = connect_lake()
    columns = ", ".join([*FEATURES, LABEL, *EXTRA])
    frame = con.sql(
        f"""
        select {columns}
        from read_parquet('{lake_glob("gold", "inflight_training_set")}', hive_partitioning = true)
        """
    ).df()
    return prepare(frame)


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in CATEGORICAL:
        frame[column] = frame[column].astype("category")
    for column in frame.columns:
        if frame[column].dtype == "boolean" or frame[column].dtype == bool:
            frame[column] = frame[column].astype("float32")
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def split_by_time(frame: pd.DataFrame) -> Split:
    """Oldest days train, the next VALID_DAYS validate, the newest TEST_DAYS test. A flight's
    moments all share its takeoff day, so no flight is on both sides."""
    days = np.sort(frame["date"].unique())
    test_from, valid_from = days[-TEST_DAYS], days[-TEST_DAYS - VALID_DAYS]
    return Split(
        train=frame[frame["date"] < valid_from],
        valid=frame[(frame["date"] >= valid_from) & (frame["date"] < test_from)],
        test=frame[frame["date"] >= test_from],
    )


def band(minutes: pd.Series | np.ndarray) -> pd.Series:
    labels = [f"{int(lo)}-{int(hi)}" if np.isfinite(hi) else f"{int(lo)}+" for lo, hi in
              zip(BANDS[:-1], BANDS[1:], strict=True)]  # fmt: skip
    return pd.cut(pd.Series(np.asarray(minutes)), BANDS, labels=labels, right=False)


# --- baselines: simple estimates with a range from their own errors on the training days ---


def direct(rows: pd.DataFrame) -> np.ndarray:
    """Distance left / current ground speed."""
    return np.asarray(rows["direct_estimate_min"].to_numpy(dtype=float, na_value=np.nan))


def usual_remaining(rows: pd.DataFrame) -> np.ndarray:
    """The route's usual air time minus time flown (never below 0); distance / speed when the
    route has no BTS history."""
    remaining = rows["route_remaining_min"].to_numpy(dtype=float, na_value=np.nan)
    return np.where(np.isnan(remaining), direct(rows), np.maximum(remaining, 0))


def with_range(train: pd.DataFrame, rows: pd.DataFrame, estimate: object) -> pd.DataFrame:
    """P50 = the estimate; P10/P90 = the estimate plus its 10th/90th percentile miss on the
    training days (so the baseline's range is honest too)."""
    assert callable(estimate)
    miss = train[LABEL].to_numpy(dtype=float) - estimate(train)
    low, high = np.nanquantile(miss, [0.1, 0.9])
    p50 = estimate(rows)
    p50 = np.where(np.isnan(p50), np.nanmedian(train[LABEL]), p50)
    return pd.DataFrame({"p10": p50 + low, "p50": p50, "p90": p50 + high}, index=rows.index)
