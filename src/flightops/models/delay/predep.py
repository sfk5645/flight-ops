"""Pre-departure delay model: three LightGBM models, one per quantile (P10, P50, P90).

The in-flight model (minutes to landing) is trained the same way with its own label and
inputs, so `train` and `calibrate` take those as arguments (see models/delay/inflight.py).

Quantile regression ("pinball" loss) learns a value that the true delay falls below 10%, 50%
or 90% of the time, so P10..P90 is an 80% range. Training stops when the validation months stop
improving (early stopping), so the number of trees is chosen on data the model didn't train on.

In practice the raw P10..P90 range holds fewer than 80% of flights (about 76%): each tree fits
its training months a little too well, so the range comes out too sure of itself. `calibrate`
measures on the validation months how far each end is off and moves that end by a fixed number
of minutes (the idea behind "conformalized quantile regression"). The two ends are fixed
separately because delays are lopsided: the late end usually needs to move more.
"""

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

from flightops.models.delay.features import CATEGORICAL, LABEL

QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}

PARAMS = {
    "objective": "quantile",
    "learning_rate": 0.1,
    "num_leaves": 127,
    "min_data_in_leaf": 500,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "max_cat_to_onehot": 4,
    "cat_smooth": 50,
    "verbose": -1,
    "seed": 7,
    "deterministic": True,
    "force_row_wise": True,
}
MAX_ROUNDS = 1000
MIN_BAND_ROWS = 1000
PATIENCE = 50


@dataclass
class QuantileModel:
    features: list[str]
    boosters: dict[str, lgb.Booster]
    # minutes added to each quantile's prediction, one value per band of the raw P50 (from
    # `calibrate`; empty = raw model). band_edges are the inner band edges in minutes: none
    # means a single band (the same shift for every row).
    shifts: dict[str, list[float]] = field(default_factory=dict)
    band_edges: list[float] = field(default_factory=list)

    def predict(self, rows: pd.DataFrame, calibrated: bool = True) -> pd.DataFrame:
        predicted = pd.DataFrame(
            {
                name: np.asarray(booster.predict(rows[self.features]))
                for name, booster in self.boosters.items()
            },
            index=rows.index,
        )
        if calibrated and self.shifts:
            band = self.band_of(predicted["p50"].to_numpy())
            for name, shifts in self.shifts.items():
                predicted[name] += np.asarray(shifts)[band]
        # Three separate models can cross (P10 above P50). The ends are pulled to the P50 rather
        # than sorting the three: sorting could swap an end into the middle and break the P50's
        # monotone guarantee (see train).
        if "p50" in predicted:
            if "p10" in predicted:
                predicted["p10"] = np.minimum(predicted["p10"], predicted["p50"])
            if "p90" in predicted:
                predicted["p90"] = np.maximum(predicted["p90"], predicted["p50"])
        return predicted

    def band_of(self, raw_p50: np.ndarray) -> np.ndarray:
        return np.searchsorted(np.asarray(self.band_edges, dtype=float), raw_p50, side="right")


def train(
    train_rows: pd.DataFrame,
    valid_rows: pd.DataFrame,
    features: list[str],
    quantiles: dict[str, float] = QUANTILES,
    label: str = LABEL,
    categorical: list[str] = CATEGORICAL,
    calibration_bands: list[float] | None = None,
    monotone: dict[str, int] | None = None,
    huber_delta: float = 10.0,
    max_rounds: int = MAX_ROUNDS,
) -> QuantileModel:
    """`monotone` forces a direction on inputs of the P50, e.g. {"dest_thunderstorm": 1}:
    raising it can never lower the P50 (-1: never raise it). The promotion gates' behaviour
    tests demand this: without it, ~8% of flights got an *earlier* prediction from a
    destination storm or a later inbound plane, and 3% of in-flight moments an earlier landing
    from being further away.

    LightGBM refuses monotone constraints for objectives that re-fit leaves to a quantile
    (quantile, L1), so a constrained P50 uses the Huber loss instead: squared error for misses
    under `huber_delta` minutes, absolute error beyond, so it stays close to a median and long
    delays can't drag it. Measured Nov 2026 on 1M-row samples: pre-departure 25.57 -> 25.63
    min, in-flight 6.51 -> 6.78 (delta 10 and 5). P10/P90 stay quantile models without
    constraints; calibration only moves them, never the P50, so the guarantee holds for the P50.
    """
    categorical = [c for c in features if c in categorical]
    boosters = {}
    for name, alpha in quantiles.items():
        params: dict[str, object] = {**PARAMS, "alpha": alpha}
        if monotone and name == "p50":
            params |= {
                "objective": "huber",
                "alpha": huber_delta,
                "metric": "l1",
                "monotone_constraints": [monotone.get(c, 0) for c in features],
                # "advanced" still broke the inbound rule for 30 of 20,000 flights (Nov 23 2026,
                # 1M-row sample); "intermediate" broke none and was the most accurate
                # (25.628 vs advanced 25.633, basic 25.648 min)
                "monotone_constraints_method": "intermediate",
            }
        train_set = lgb.Dataset(
            train_rows[features], train_rows[label], categorical_feature=categorical
        )
        valid_set = lgb.Dataset(valid_rows[features], valid_rows[label], reference=train_set)
        boosters[name] = lgb.train(
            params,
            train_set,
            num_boost_round=max_rounds,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(PATIENCE, verbose=False)],
        )
    model = QuantileModel(features, boosters, band_edges=list(calibration_bands or []))
    model.shifts = calibrate(model, valid_rows, quantiles, label)
    return model


def calibrate(
    model: QuantileModel,
    valid_rows: pd.DataFrame,
    quantiles: dict[str, float] = QUANTILES,
    label: str = LABEL,
) -> dict[str, list[float]]:
    """Minutes to add to each end of the range so it holds its share on the validation data.

    For P10: of all validation rows, how far below or above the raw P10 did they land? The
    10th percentile of those misses is the shift that puts exactly 10% below the new P10.
    Likewise the 90th percentile for P90. The P50 is left alone: it is judged on its error,
    not on coverage. Measured on validation data, never the test data, so the test score
    stays an honest check.

    With model.band_edges, rows are grouped by the model's own raw P50 (what is known live,
    unlike the true answer) and each band gets its own shift: the in-flight model's misses are
    much smaller 10 minutes out than 3 hours out, so one shift would make the range too wide
    near landing and too narrow far away. A band with fewer than MIN_BAND_ROWS validation rows
    uses the overall shift.
    """
    raw = model.predict(valid_rows, calibrated=False)
    actual = valid_rows[label].to_numpy(dtype=float)
    band = model.band_of(raw["p50"].to_numpy()) if "p50" in raw else np.zeros(len(raw), int)
    shifts: dict[str, list[float]] = {}
    for name, alpha in quantiles.items():
        if name == "p50":
            continue
        miss = actual - raw[name].to_numpy()
        overall = float(np.quantile(miss, alpha))
        shifts[name] = [
            float(np.quantile(miss[band == b], alpha))
            if (band == b).sum() >= MIN_BAND_ROWS
            else overall
            for b in range(len(model.band_edges) + 1)
        ]
    return shifts
