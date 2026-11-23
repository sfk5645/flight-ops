"""Train the pre-departure delay model and compare it with the baselines on the newest months.

    uv run --env-file .env python -m flightops.flows.train_predep [--sample 0.2] [--groups]
        [--breakdown] [--training-set PATH] [--register]

Reads gold/training_set from R2 into memory. With --register, the model is saved as a new
version of `predep-delay` in the MLflow registry (DagsHub), with its test scores; otherwise
nothing is written.
Prints Markdown tables: baselines vs model on the test months, the most used inputs, and with
--groups, how much each group of inputs adds (P50 only, on a sample, to keep it quick).
"""

import argparse
import logging
import time

import pandas as pd
import pyarrow as pa

from flightops.flows.validate_landings import markdown
from flightops.models import registry
from flightops.models.delay import baselines, breakdown, predep
from flightops.models.delay.features import (
    CATEGORICAL,
    FEATURE_GROUPS,
    LABEL,
    MONOTONE,
    Split,
    load_training_set,
    split_by_time,
)
from flightops.models.delay.metrics import score

log = logging.getLogger(__name__)

GROUPS_SAMPLE_ROWS = 2_000_000


def scores_table(split: Split, predictions: dict[str, pd.DataFrame]) -> pd.DataFrame:
    actual = split.test[LABEL].to_numpy(dtype=float)
    rows = []
    for name, p in predictions.items():
        s = score(actual, p["p10"].to_numpy(), p["p50"].to_numpy(), p["p90"].to_numpy())
        rows.append(
            {
                "predictor": name,
                "P50 error (min)": round(s["mae"], 2),
                "within 15 min": f"{s['within_15']:.1%}",
                "in P10-P90": f"{s['coverage']:.1%}",
                "range width (min)": round(s["width"], 1),
                "pinball": round(s["pinball"], 3),
            }
        )
    return pd.DataFrame(rows)


def group_gains(split: Split) -> pd.DataFrame:
    """Add one group at a time (P50 model only) and record the error on the test months."""
    train = split.train.sample(min(GROUPS_SAMPLE_ROWS, len(split.train)), random_state=7)
    actual = split.test[LABEL].to_numpy(dtype=float)
    rows: list[dict[str, object]] = []
    features: list[str] = []
    for group, columns in FEATURE_GROUPS.items():
        features = [*features, *columns]
        model = predep.train(train, split.valid, features, quantiles={"p50": 0.5})
        p50 = model.predict(split.test)["p50"].to_numpy()
        error = abs(actual - p50)
        rows.append(
            {
                "inputs": " + ".join(list(FEATURE_GROUPS)[: list(FEATURE_GROUPS).index(group) + 1]),
                "P50 error (min)": round(float(error.mean()), 2),
                "within 15 min": f"{(error <= 15).mean():.1%}",
                "trees": model.boosters["p50"].best_iteration,
            }
        )
        log.info("groups: %s", rows[-1])
    without = [c for c in features if c not in FEATURE_GROUPS["inbound"]]
    model = predep.train(train, split.valid, without, quantiles={"p50": 0.5})
    error = abs(actual - model.predict(split.test)["p50"].to_numpy())
    rows.append(
        {
            "inputs": "everything except inbound",
            "P50 error (min)": round(float(error.mean()), 2),
            "within 15 min": f"{(error <= 15).mean():.1%}",
            "trees": model.boosters["p50"].best_iteration,
        }
    )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=float, help="train on this share of training rows")
    parser.add_argument("--groups", action="store_true", help="also measure each input group")
    parser.add_argument(
        "--training-set", help="local training_set folder (another prediction lead) instead of R2"
    )
    parser.add_argument(
        "--breakdown", action="store_true", help="also split the test error into slices"
    )
    parser.add_argument(
        "--register", action="store_true", help="save the model to the MLflow registry"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    started = time.monotonic()
    split = split_by_time(load_training_set(path=args.training_set))
    if args.sample:
        split = Split(split.train.sample(frac=args.sample, random_state=7), split.valid, split.test)
    for name in ("train", "valid", "test"):
        part = getattr(split, name)
        log.info(
            "%s: %s rows, %s to %s", name, len(part), part.flight_date.min(), part.flight_date.max()
        )

    features = [c for group in FEATURE_GROUPS.values() for c in group]
    model = predep.train(split.train, split.valid, features, monotone=MONOTONE)
    log.info(
        "model trained in %.0f s, trees: %s, range shifts (min): %s",
        time.monotonic() - started,
        {k: b.best_iteration for k, b in model.boosters.items()},
        {k: [round(x, 1) for x in v] for k, v in model.shifts.items()},
    )
    predictions = {
        "always 0 min": baselines.zero(split.train, split.test),
        "route + hour history": baselines.route_hour(split.train, split.test),
        "LightGBM, raw range": model.predict(split.test, calibrated=False),
        "LightGBM": model.predict(split.test),
    }
    print("## Test months\n")
    print(markdown_frame(scores_table(split, predictions)))

    importance = pd.Series(
        model.boosters["p50"].feature_importance("gain"), index=features
    ).sort_values(ascending=False)
    share = (importance / importance.sum()).head(15)
    print("\n## Most used inputs (P50 model, share of gain)\n")
    print(
        markdown_frame(
            share.map("{:.1%}".format).rename("share").rename_axis("input").reset_index()
        )
    )

    if args.register:
        actual = split.test[LABEL].to_numpy(dtype=float)
        metrics: dict[str, float] = {}
        for name, p in predictions.items():
            prefix = {"LightGBM": "test", "LightGBM, raw range": "test_raw"}.get(
                name, "baseline_" + "_".join(name.replace("+", " ").split())
            )
            metrics |= registry.score_metrics(
                prefix,
                score(actual, p["p10"].to_numpy(), p["p50"].to_numpy(), p["p90"].to_numpy()),
            )
        version = registry.register(
            model,
            name=registry.PREDEP,
            categorical=CATEGORICAL,
            sample_rows=split.train,
            metrics=metrics,
            params=split_params(split, "flight_date") | {"sample": args.sample or 1.0},
        )
        log.info("registered %s version %s", registry.PREDEP, version)

    if args.breakdown:
        actual = split.test[LABEL].to_numpy(dtype=float)
        p50 = predictions["LightGBM"]["p50"].to_numpy()
        print("\n## How concentrated the error is\n")
        print(markdown_frame(breakdown.concentration(actual, p50)))
        tables = breakdown.breakdown(
            split.test, p50, predictions["route + hour history"]["p50"].to_numpy()
        )
        for name, table in tables.items():
            print(f"\n## Error by {name}\n")
            print(markdown_frame(table))

    if args.groups:
        print("\n## What each group adds (P50 only)\n")
        print(markdown_frame(group_gains(split)))
    log.info("done in %.0f s", time.monotonic() - started)


def split_params(split: object, date_column: str) -> dict[str, object]:
    """Where the model's data came from: each part's rows and first and last day (`split`
    is either model's Split: anything with train, valid and test tables)."""
    params: dict[str, object] = {}
    for name in ("train", "valid", "test"):
        part = getattr(split, name)
        dates = pd.to_datetime(part[date_column])
        params |= {
            f"{name}_rows": len(part),
            f"{name}_from": str(dates.min().date()),
            f"{name}_to": str(dates.max().date()),
        }
    return params


def markdown_frame(frame: pd.DataFrame) -> str:
    return markdown(pa.Table.from_pandas(frame, preserve_index=False))


if __name__ == "__main__":
    main()
