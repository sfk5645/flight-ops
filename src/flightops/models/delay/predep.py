"""Pre-departure delay model: three LightGBM models, one per quantile (P10, P50, P90).

Quantile regression ("pinball" loss) learns a value that the true delay falls below 10%, 50%
or 90% of the time, so P10..P90 is an 80% range. Training stops when the validation months stop
improving (early stopping), so the number of trees is chosen on data the model didn't train on.
"""

from dataclasses import dataclass

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
PATIENCE = 50


@dataclass
class QuantileModel:
    features: list[str]
    boosters: dict[str, lgb.Booster]

    def predict(self, rows: pd.DataFrame) -> pd.DataFrame:
        predicted = pd.DataFrame(
            {name: booster.predict(rows[self.features]) for name, booster in self.boosters.items()},
            index=rows.index,
        )
        # Three separate models can cross (P10 above P50); sorting each row fixes the order.
        predicted[list(self.boosters)] = np.sort(predicted[list(self.boosters)].to_numpy(), axis=1)
        return predicted


def train(
    train_rows: pd.DataFrame,
    valid_rows: pd.DataFrame,
    features: list[str],
    quantiles: dict[str, float] = QUANTILES,
) -> QuantileModel:
    categorical = [c for c in features if c in CATEGORICAL]
    boosters = {}
    for name, alpha in quantiles.items():
        train_set = lgb.Dataset(
            train_rows[features], train_rows[LABEL], categorical_feature=categorical
        )
        valid_set = lgb.Dataset(valid_rows[features], valid_rows[LABEL], reference=train_set)
        boosters[name] = lgb.train(
            {**PARAMS, "alpha": alpha},
            train_set,
            num_boost_round=MAX_ROUNDS,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(PATIENCE, verbose=False)],
        )
    return QuantileModel(features, boosters)
