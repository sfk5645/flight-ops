"""Simple predictions the model must beat. Both learn only from the training months.

zero           P50 = 0 minutes late; the range is the spread of all training flights
route_hour     P10/P50/P90 of the same route at the same scheduled hour in the training months,
               falling back to the route, then to all flights, when there are fewer than
               MIN_FLIGHTS examples
"""

import numpy as np
import pandas as pd

from flightops.models.delay.features import LABEL

QUANTILES = (0.1, 0.5, 0.9)
MIN_FLIGHTS = 30


def zero(train: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    p10, _, p90 = np.quantile(train[LABEL], QUANTILES)
    n = len(rows)
    return pd.DataFrame(
        {"p10": np.full(n, p10), "p50": np.zeros(n), "p90": np.full(n, p90)}, index=rows.index
    )


def route_hour(train: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    overall = np.quantile(train[LABEL], QUANTILES)
    result = pd.DataFrame(
        {"p10": overall[0], "p50": overall[1], "p90": overall[2]}, index=rows.index
    )
    # coarse to fine: each finer level overwrites where it has enough flights
    for keys in (["origin", "dest"], ["origin", "dest", "sched_dep_hour_local"]):
        table = _quantile_table(train, keys)
        matched = rows[keys].astype(object).merge(table, how="left", left_on=keys, right_index=True)
        found = matched["p50"].notna().to_numpy()
        result.loc[found, ["p10", "p50", "p90"]] = matched.loc[found, ["p10", "p50", "p90"]].values
    return result


def _quantile_table(train: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    grouped = train[[*keys, LABEL]].astype({k: object for k in keys}).groupby(keys)[LABEL]
    table = grouped.quantile(np.array(QUANTILES)).unstack()
    table.columns = ["p10", "p50", "p90"]
    return table[grouped.size() >= MIN_FLIGHTS]
