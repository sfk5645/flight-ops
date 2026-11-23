"""Train the in-flight model (minutes to landing) and compare it with the baselines.

    uv run --env-file .env python -m flightops.flows.train_inflight [--sample 0.2] [--groups]
        [--register]

Reads gold/inflight_training_set from R2 into memory. With --register, the model is saved as a
new version of `inflight-landing` in the MLflow registry (DagsHub), with its test scores
(overall, per minutes-to-landing band and per feed); otherwise nothing is written.

Prints Markdown tables: baselines vs model on the newest days, the same by minutes remaining
(the last half hour is where < 5 minutes is expected) and by feed, and the most used inputs.
With --groups, also how much each group of inputs adds (P50 only, on a sample).
"""

import argparse
import logging
import time

import numpy as np
import pandas as pd

from flightops.flows.train_predep import markdown_frame, split_params
from flightops.models import registry
from flightops.models.delay import inflight, predep
from flightops.models.delay.inflight import LABEL, Split
from flightops.models.delay.metrics import score

log = logging.getLogger(__name__)

GROUPS_SAMPLE_ROWS = 1_000_000


def score_row(name: str, rows: pd.DataFrame, p: pd.DataFrame) -> dict[str, object]:
    s = score(
        rows[LABEL].to_numpy(dtype=float),
        p["p10"].to_numpy(),
        p["p50"].to_numpy(),
        p["p90"].to_numpy(),
    )
    return {
        "predictor": name,
        "moments": len(rows),
        "P50 error (min)": round(s["mae"], 2),
        "within 5 min": f"{s['within_5']:.1%}",
        "within 15 min": f"{s['within_15']:.1%}",
        "in P10-P90": f"{s['coverage']:.1%}",
        "range width (min)": round(s["width"], 1),
        "pinball": round(s["pinball"], 3),
    }


def sliced(test: pd.DataFrame, predictions: dict[str, pd.DataFrame], by: pd.Series) -> pd.DataFrame:
    rows = []
    for value in by.cat.categories if hasattr(by, "cat") else sorted(by.unique()):
        mask = (by == value).to_numpy()
        if not mask.any():
            continue
        for name, p in predictions.items():
            rows.append({"slice": str(value), **score_row(name, test[mask], p[mask])})
    return pd.DataFrame(rows)


def group_gains(split: Split) -> pd.DataFrame:
    """Add one group at a time (P50 model only) and score each on the test days, overall and in
    the last hour before landing."""
    train = split.train.sample(min(GROUPS_SAMPLE_ROWS, len(split.train)), random_state=7)
    actual = split.test[LABEL].to_numpy(dtype=float)
    last_hour = actual < 60
    rows: list[dict[str, object]] = []
    names = list(inflight.FEATURE_GROUPS)
    for i in range(len(names)):
        features = [c for g in names[: i + 1] for c in inflight.FEATURE_GROUPS[g]]
        model = predep.train(
            train,
            split.valid,
            features,
            quantiles={"p50": 0.5},
            label=LABEL,
            categorical=inflight.CATEGORICAL,
        )
        error = abs(actual - model.predict(split.test)["p50"].to_numpy())
        rows.append(
            {
                "inputs": " + ".join(names[: i + 1]),
                "P50 error (min)": round(float(error.mean()), 2),
                "within 5 min": f"{(error <= 5).mean():.1%}",
                "last hour: error": round(float(error[last_hour].mean()), 2),
                "last hour: within 5": f"{(error[last_hour] <= 5).mean():.1%}",
            }
        )
        log.info("groups: %s", rows[-1])
    return pd.DataFrame(rows)


def inflight_metrics(test: pd.DataFrame, predictions: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Test scores for the registry: model and baselines overall, the model per minutes-to-
    landing band (e.g. test_mae_0_15) and per feed (e.g. test_mae_opensky)."""
    actual = test[LABEL].to_numpy(dtype=float)

    def scores(mask: np.ndarray, p: pd.DataFrame) -> dict[str, float]:
        return score(
            actual[mask], p["p10"].to_numpy()[mask], p["p50"].to_numpy()[mask],
            p["p90"].to_numpy()[mask],
        )  # fmt: skip

    everything = np.ones(len(test), dtype=bool)
    prefixes = {
        "LightGBM": "test",
        "LightGBM, raw range": "test_raw",
        "distance / speed": "baseline_distance_speed",
        "usual air time - flown": "baseline_usual_remaining",
    }
    metrics: dict[str, float] = {}
    for name, p in predictions.items():
        metrics |= registry.score_metrics(prefixes[name], scores(everything, p))
    model = predictions["LightGBM"]
    bands = inflight.band(test[LABEL]).astype(str).to_numpy()
    for value in np.unique(bands):
        tag = value.replace("-", "_").replace("+", "_plus")
        for key, number in scores(bands == value, model).items():
            if key in ("mae", "within_5", "coverage"):
                metrics[f"test_{key}_{tag}"] = round(number, 4)
    feeds = test["feed"].astype(str).to_numpy()
    for value in np.unique(feeds):
        for key, number in scores(feeds == value, model).items():
            if key in ("mae", "within_5", "coverage"):
                metrics[f"test_{key}_{value}"] = round(number, 4)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=float, help="train on this share of training rows")
    parser.add_argument("--groups", action="store_true", help="also measure each input group")
    parser.add_argument(
        "--register", action="store_true", help="save the model to the MLflow registry"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    started = time.monotonic()
    split = inflight.split_by_time(inflight.load_training_set())
    if args.sample:
        split = Split(split.train.sample(frac=args.sample, random_state=7), split.valid, split.test)
    for name in ("train", "valid", "test"):
        part = getattr(split, name)
        log.info(
            "%s: %d moments, %s to %s",
            name,
            len(part),
            part["date"].min().date(),
            part["date"].max().date(),
        )

    model = predep.train(
        split.train,
        split.valid,
        inflight.FEATURES,
        label=LABEL,
        categorical=inflight.CATEGORICAL,
        calibration_bands=inflight.CALIBRATION_BANDS,
        monotone=inflight.MONOTONE,
        max_rounds=inflight.MAX_ROUNDS,
    )
    log.info(
        "model trained in %.0f s, trees: %s, range shifts (min): %s",
        time.monotonic() - started,
        {k: b.best_iteration for k, b in model.boosters.items()},
        {k: [round(x, 1) for x in v] for k, v in model.shifts.items()},
    )
    test = split.test.reset_index(drop=True)
    predictions = {
        "distance / speed": inflight.with_range(split.train, test, inflight.direct),
        "usual air time - flown": inflight.with_range(split.train, test, inflight.usual_remaining),
        "LightGBM, raw range": model.predict(test, calibrated=False).reset_index(drop=True),
        "LightGBM": model.predict(test).reset_index(drop=True),
    }
    print("## Test days\n")
    print(markdown_frame(pd.DataFrame([score_row(n, test, p) for n, p in predictions.items()])))

    compared = {k: predictions[k] for k in ("usual air time - flown", "LightGBM")}
    print("\n## By minutes to landing\n")
    print(markdown_frame(sliced(test, compared, inflight.band(test[LABEL]))))
    print("\n## By feed\n")
    print(markdown_frame(sliced(test, compared, test["feed"])))
    print("\n## By destination guess\n")
    print(
        markdown_frame(
            sliced(test, compared, test["dest_guess_right"].map({1.0: "right", 0.0: "wrong"}))
        )
    )

    importance = pd.Series(
        model.boosters["p50"].feature_importance("gain"), index=inflight.FEATURES
    ).sort_values(ascending=False)
    share = importance / importance.sum()
    print("\n## Most used inputs (P50 model, share of gain)\n")
    print(
        markdown_frame(
            share.map("{:.1%}".format).rename("share").rename_axis("input").reset_index()
        )
    )
    if args.register:
        version = registry.register(
            model,
            name=registry.INFLIGHT,
            categorical=inflight.CATEGORICAL,
            sample_rows=split.train,
            metrics=inflight_metrics(test, predictions),
            params=split_params(split, "date") | {"sample": args.sample or 1.0},
        )
        log.info("registered %s version %s", registry.INFLIGHT, version)

    if args.groups:
        print("\n## What each group adds (P50 only)\n")
        print(markdown_frame(group_gains(split)))
    log.info("done in %.0f s", time.monotonic() - started)


if __name__ == "__main__":
    main()
