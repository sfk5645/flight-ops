"""Run the promotion gates on a registered model version, and promote it if all pass.

    uv run --env-file .env python -m flightops.flows.evaluate_model \
        --model inflight-landing --version 2 [--promote]

The version is loaded back from the registry (DagsHub) and scored on the test days recorded
with it at training (test_from .. test_to: never trained or tuned on), next to the current
champion on the same rows. Prints a table of gates (flightops.models.gates) and records the
outcome on the version as tags. With --promote and every gate passed, the version gets the
`challenger` alias, and also `champion` when there is none yet (the first model). Later
champions change through the release process (shadow mode for in-flight), never here.
"""

import argparse
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from flightops.flows.train_predep import markdown_frame
from flightops.models import gates, registry
from flightops.models.delay import features as predep_features
from flightops.models.delay import inflight

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
log = logging.getLogger(__name__)

BEHAVIOUR_ROWS = 5_000


@dataclass(frozen=True)
class ModelSpec:
    features: list[str]
    categorical: list[str]
    label: str
    date_column: str
    segments: list[str]
    behaviours: list[gates.Behaviour]
    load: Callable[[str, str], pd.DataFrame]


def _set(column: str, value: float) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def change(rows: pd.DataFrame) -> pd.DataFrame:
        rows[column] = value
        return rows

    return change


def _later_inbound(rows: pd.DataFrame) -> pd.DataFrame:
    rows["inbound_buffer_min"] = rows["inbound_buffer_min"] - 30
    return rows


def _load_predep(first: str, last: str) -> pd.DataFrame:
    rows = predep_features.load_training_set(first_month=first[:7])
    dates = pd.to_datetime(rows["flight_date"])
    return rows[(dates >= pd.Timestamp(first)) & (dates <= pd.Timestamp(last))]


def _load_inflight(first: str, last: str) -> pd.DataFrame:
    rows = inflight.load_training_set()
    return rows[(rows["date"] >= pd.Timestamp(first)) & (rows["date"] <= pd.Timestamp(last))]


SPECS = {
    registry.PREDEP: ModelSpec(
        features=predep_features.FEATURES,
        categorical=predep_features.CATEGORICAL,
        label=predep_features.LABEL,
        date_column="flight_date",
        segments=["origin", "dest", "carrier"],
        behaviours=[
            gates.Behaviour(
                "thunderstorm at the destination never makes it earlier",
                lambda r: r["dest_thunderstorm"] == 0,
                _set("dest_thunderstorm", 1.0),
            ),
            gates.Behaviour(
                "thunderstorm at the origin never makes it earlier",
                lambda r: r["origin_thunderstorm"] == 0,
                _set("origin_thunderstorm", 1.0),
            ),
            gates.Behaviour(
                "an inbound plane 30 min later never makes it earlier",
                lambda r: r["inbound_buffer_min"].notna(),
                _later_inbound,
            ),
        ],
        load=_load_predep,
    ),
    registry.INFLIGHT: ModelSpec(
        features=inflight.FEATURES,
        categorical=inflight.CATEGORICAL,
        label=inflight.LABEL,
        date_column="date",
        segments=["dest", "carrier"],
        # no forced directions yet (see inflight.MONOTONE); destination weather will add one
        behaviours=[],
        load=_load_inflight,
    ),
}


def evaluate(name: str, version: str) -> tuple[list[gates.Gate], str | None]:
    """All gates for one version. Returns them and the current champion's version (or None)."""
    import mlflow

    spec = SPECS[name]
    client = mlflow.MlflowClient()
    run_id = client.get_model_version(name, version).run_id
    assert run_id, f"{name} v{version} has no run"
    run = client.get_run(run_id)
    first, last = run.data.params["test_from"], run.data.params["test_to"]
    log.info("%s v%s: test days %s to %s", name, version, first, last)

    candidate = mlflow.pyfunc.load_model(f"models:/{name}/{version}")
    try:
        champion_version: str | None = client.get_model_version_by_alias(name, "champion").version
    except mlflow.exceptions.MlflowException:
        champion_version = None
    if champion_version == version:
        champion_version = None  # comparing a model with itself proves nothing
    champion = (
        mlflow.pyfunc.load_model(f"models:/{name}/{champion_version}") if champion_version else None
    )

    def predict_with(model: object) -> gates.Predict:
        def predict(rows: pd.DataFrame) -> pd.DataFrame:
            sent = registry.signature_example(rows[spec.features], spec.categorical)
            predicted: pd.DataFrame = model.predict(sent)  # type: ignore[attr-defined]
            return predicted.reset_index(drop=True)

        return predict

    rows = spec.load(first, last).reset_index(drop=True)
    actual = rows[spec.label].to_numpy(dtype=float)
    predict = predict_with(candidate)
    p = predict(rows)
    champion_p = predict_with(champion)(rows) if champion else None
    log.info("scored %d rows", len(rows))

    logged = run.data.metrics
    schema = candidate.metadata.get_input_schema()
    logged_inputs = {c.name: c.type.name for c in schema.inputs} if schema else None
    expected = {c: "string" if c in spec.categorical else "double" for c in spec.features}
    sample = rows.sample(min(BEHAVIOUR_ROWS, len(rows)), random_state=7)

    results = [
        gates.loads_and_reproduces(float(np.mean(np.abs(actual - p["p50"]))), logged["test_mae"]),
        gates.beats_baselines(logged),
        gates.coverage(actual, p),
        gates.beats_champion(actual, p, champion_p),
        gates.no_segment_worse(
            rows,
            actual,
            p["p50"].to_numpy(),
            None if champion_p is None else champion_p["p50"].to_numpy(),
            spec.segments,
        ),
        *gates.behaviour(predict, sample, spec.behaviours),
        gates.signature(expected, logged_inputs),
        gates.speed(predict, rows),
    ]
    return results, champion_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=sorted(SPECS), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--promote", action="store_true", help="set aliases if every gate passes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import mlflow

    results, champion = evaluate(args.model, args.version)
    print(f"## Gates: {args.model} version {args.version}\n")
    print(
        markdown_frame(
            pd.DataFrame(
                [
                    {"gate": g.name, "result": "PASS" if g.passed else "FAIL", "detail": g.detail}
                    for g in results
                ]
            )
        )
    )
    passed = all(g.passed for g in results)
    client = mlflow.MlflowClient()
    client.set_model_version_tag(
        args.model, args.version, "gates", "passed" if passed else "failed"
    )
    failed = [g.name for g in results if not g.passed]
    if failed:
        client.set_model_version_tag(args.model, args.version, "gates_failed", ", ".join(failed))
    print(f"\n{'All gates passed.' if passed else 'Failed: ' + ', '.join(failed)}")

    if args.promote and passed:
        client.set_registered_model_alias(args.model, "challenger", args.version)
        print(f"{args.model} version {args.version} is now the challenger.")
        if champion is None and not _has_champion(client, args.model):
            client.set_registered_model_alias(args.model, "champion", args.version)
            print(f"No champion existed: version {args.version} is now the champion.")


def _has_champion(client: object, name: str) -> bool:
    import mlflow

    try:
        client.get_model_version_by_alias(name, "champion")  # type: ignore[attr-defined]
    except mlflow.exceptions.MlflowException:
        return False
    return True


if __name__ == "__main__":
    main()
