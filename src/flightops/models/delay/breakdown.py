"""Where the P50 error comes from: the test flights split into slices, each slice's share of
flights next to its share of the total error. A slice with 5% of flights and 30% of the error
is where improvements would pay off; the baseline column shows where the model already helps.

Slices only use what is known before departure (so they point at inputs we could improve),
except "actual delay", which shows how much of the error the rare very late flights cause.
"""

import numpy as np
import pandas as pd

from flightops.models.delay.features import LABEL

DELAY_BINS = [-np.inf, -15, 15, 60, 180, np.inf]
DELAY_LABELS = ["early 15+", "within 15", "15-60 late", "60-180 late", "180+ late"]


def slices(rows: pd.DataFrame) -> dict[str, pd.Series]:
    """Each value labels every row with its slice, e.g. {"inbound status": landed/airborne/...}."""
    hour = rows["sched_dep_hour_local"]
    storm = _either(rows, "thunderstorm", 1)
    low_cloud = _either(rows, "flight_category", 2)
    snow = _either(rows, "snow_or_ice", 1)
    return {
        "actual delay": pd.cut(rows[LABEL], DELAY_BINS, labels=DELAY_LABELS),
        "inbound status": rows["inbound_status"].astype(object).fillna("no guess"),
        "inbound guess confidence (days same of 7)": pd.cut(
            rows["inbound_days_same"], [0, 2, 4, 6, 7], labels=["1-2", "3-4", "5-6", "7"]
        )
        .astype(object)
        .fillna("no guess"),
        "inbound buffer (min)": pd.cut(
            rows["inbound_buffer_min"],
            [-np.inf, 0, 30, 60, 120, np.inf],
            labels=["< 0", "0-30", "30-60", "60-120", "120+"],
        )
        .astype(object)
        .fillna("unknown"),
        "scheduled departure (local)": pd.cut(
            hour, [-1, 4, 11, 16, 20, 23], labels=["00-04", "05-11", "12-16", "17-20", "21-23"]
        ),
        "weather at either end": pd.Series(
            np.select(
                [storm, snow, low_cloud], ["thunderstorm", "snow / ice", "IFR or LIFR"], "fine"
            ),
            index=rows.index,
        ),
        "month": rows["flight_date"].astype(str).str[:7],
        "carrier": rows["carrier"].astype(object),
    }


def _either(rows: pd.DataFrame, name: str, at_least: float) -> np.ndarray:
    """origin_<name> or dest_<name> >= at_least; missing reports count as no."""
    hit = (rows[f"origin_{name}"] >= at_least) | (rows[f"dest_{name}"] >= at_least)
    return hit.fillna(False).to_numpy(dtype=bool)


def breakdown(
    rows: pd.DataFrame, p50: np.ndarray, baseline_p50: np.ndarray, top: int = 8
) -> dict[str, pd.DataFrame]:
    """For each slice: share of flights, share of total error, model and baseline error, and
    bias (average actual minus P50: positive means the model predicts too early)."""
    actual = rows[LABEL].to_numpy(dtype=float)
    frame = pd.DataFrame(
        {
            "error": np.abs(actual - p50),
            "baseline_error": np.abs(actual - baseline_p50),
            "bias": actual - p50,
        },
        index=rows.index,
    )
    total = frame["error"].sum()
    tables = {}
    for name, labels in slices(rows).items():
        grouped = frame.groupby(labels.to_numpy(), observed=True)
        table = pd.DataFrame(
            {
                "flights": grouped.size() / len(frame),
                "error share": grouped["error"].sum() / total,
                "model error": grouped["error"].mean(),
                "baseline error": grouped["baseline_error"].mean(),
                "bias": grouped["bias"].mean(),
            }
        )
        if name == "carrier":
            table = table.sort_values("error share", ascending=False).head(top)
        tables[name] = _format(table.rename_axis(name).reset_index())
    return tables


def concentration(actual: np.ndarray, p50: np.ndarray) -> pd.DataFrame:
    """How much of the total error comes from the worst 1/5/10/25% of flights."""
    error = np.sort(np.abs(actual - p50))[::-1]
    total = error.sum()
    rows = []
    for share in (0.01, 0.05, 0.10, 0.25):
        n = max(1, int(len(error) * share))
        rows.append(
            {
                "worst flights": f"{share:.0%}",
                "share of total error": f"{error[:n].sum() / total:.1%}",
                "smallest error in group (min)": round(float(error[n - 1]), 1),
            }
        )
    return pd.DataFrame(rows)


def _format(table: pd.DataFrame) -> pd.DataFrame:
    table = table.copy()
    for column in ("flights", "error share"):
        table[column] = table[column].map("{:.1%}".format)
    for column in ("model error", "baseline error", "bias"):
        table[column] = table[column].round(1)
    return table
