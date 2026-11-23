"""Pre-departure model inputs: which training_set columns the model sees, grouped as in
dbt/models/gold/training_set.sql, and the time-based split.

Every column here is known 1 hour (prediction_lead_min) before scheduled departure (the dbt
point-in-time test checks it). The identity columns (flight number, date, scheduled time) and
the label are not inputs.
"""

from dataclasses import dataclass

import pandas as pd

from flightops.duck import connect_lake, lake_glob

LABEL = "arr_delay_min"

FEATURE_GROUPS: dict[str, list[str]] = {
    "flight": [
        "carrier", "origin", "dest", "distance_mi", "sched_elapsed_min", "sched_dep_hour_local",
        "sched_arr_hour_local", "sched_dep_weekday_local", "month_of_year", "days_to_holiday",
    ],
    "history": [
        "route_flights_3m", "route_delay_1m", "route_delay_3m", "route_late15_3m",
        "flight_flights_3m", "flight_delay_3m", "flight_late15_3m", "carrier_delay_1m",
        "carrier_delay_3m", "carrier_late15_3m", "origin_hour_dep_delay_3m",
        "dest_hour_arr_delay_3m",
    ],
    "traffic": [
        "origin_deps_3h", "origin_deps_3h_normal", "origin_deps_3h_ratio", "dest_arrs_3h",
        "dest_arrs_3h_normal", "dest_arrs_3h_ratio",
    ],
    "today": [
        "origin_dep_late_3h", "origin_dep_late15_3h", "dest_arr_late_3h", "dest_arr_late15_3h",
        "carrier_dep_late_3h", "carrier_dep_late15_3h",
    ],
    "inbound": ["inbound_days_same", "inbound_status", "inbound_buffer_min"],
    "weather": [
        f"{end}_{name}"
        for end in ("origin", "dest")
        for name in (
            "wx_age_min", "flight_category", "category_change_3h", "thunderstorm",
            "snow_or_ice", "wind_kt", "gust_kt",
        )
    ],
}  # fmt: skip
FEATURES = [column for group in FEATURE_GROUPS.values() for column in group]
CATEGORICAL = ["carrier", "origin", "dest", "inbound_status"]

# Directions the model must respect (promotion gate behaviour tests): a thunderstorm at either
# airport never makes a flight predicted earlier; neither does a smaller turnaround buffer
# (a later inbound aircraft).
MONOTONE = {"origin_thunderstorm": 1, "dest_thunderstorm": 1, "inbound_buffer_min": -1}
# The constrained P50 (Huber loss, see predep.train) stopped at the 1,000-tree limit (Nov 23).
MAX_ROUNDS = 2500

# Delay history needs 3 published months (bts_lag_months = 3), so rows before 2025 have little
# or none of it; training starts once it is complete.
FIRST_MONTH = "2025-01"
TEST_MONTHS = 3  # newest months: the final score
VALID_MONTHS = 3  # the 3 before: early stopping and choosing settings


@dataclass(frozen=True)
class Split:
    train: pd.DataFrame
    valid: pd.DataFrame
    test: pd.DataFrame


def load_training_set(first_month: str = FIRST_MONTH, path: str | None = None) -> pd.DataFrame:
    """Read gold/training_set from R2 into memory, or a local copy built with another
    prediction_lead_min (path = its folder)."""
    con = connect_lake()
    source = f"{path.rstrip('/')}/**/*.parquet" if path else lake_glob("gold", "training_set")
    columns = ", ".join([*FEATURES, LABEL, "flight_date", "year", "month"])
    frame = con.sql(
        f"""
        select {columns}
        from read_parquet('{source}', hive_partitioning = true)
        where flight_date >= ?::date
        """,
        params=[f"{first_month}-01"],
    ).df()
    return prepare(frame)


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    """Text columns become categories (LightGBM splits on them directly); flags become 0/1."""
    frame = frame.copy()
    for column in CATEGORICAL:
        frame[column] = frame[column].astype("category")
    for column in frame.columns:
        if frame[column].dtype == "boolean" or frame[column].dtype == bool:
            frame[column] = frame[column].astype("float32")
    frame["month_key"] = frame["year"].astype(int) * 12 + frame["month"].astype(int) - 1
    return frame


def split_by_time(frame: pd.DataFrame) -> Split:
    """Oldest months train, the next VALID_MONTHS validate, the newest TEST_MONTHS test.
    Never random: a random split would let the model see the future of the months it's graded on.
    """
    newest = int(frame["month_key"].max())
    test_from = newest - TEST_MONTHS + 1
    valid_from = test_from - VALID_MONTHS
    return Split(
        train=frame[frame["month_key"] < valid_from],
        valid=frame[(frame["month_key"] >= valid_from) & (frame["month_key"] < test_from)],
        test=frame[frame["month_key"] >= test_from],
    )
