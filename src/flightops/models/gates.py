"""Promotion gates: the checks a registered model version must pass before it may be used live.

Each gate returns a Gate (name, passed, detail). flows/evaluate_model.py runs them all on the
test days recorded with the version (data it never trained or tuned on), using the model as
loaded back from the registry, and only then sets its aliases.

The gates (the project's promotion rules):
- loads_and_reproduces   the registered package predicts what training measured
- beats_baselines        lower error and pinball than every simple baseline
- coverage               P10..P90 holds 75-85% of what happened
- beats_champion         no worse pinball than the current champion on the same rows
- no_segment_worse       no top-20 airport or airline more than 2 minutes worse than the
                         champion
- behaviour              common-sense directions hold for every row (e.g. a destination
                         thunderstorm never makes a flight predicted earlier)
- signature              the inputs the model expects are exactly the ones the code sends
- speed                  a live batch scores well within the 5-minute cycle
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from flightops.models.delay.metrics import score

COVERAGE_RANGE = (0.75, 0.85)
SEGMENT_TOP = 20
SEGMENT_MAX_WORSE_MIN = 2.0
SPEED_ROWS = 10_000
SPEED_LIMIT_S = 60.0

Predict = Callable[[pd.DataFrame], pd.DataFrame]


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Behaviour:
    """A common-sense rule: after `change`, the P50 must not move against `direction`
    (+1: must not go down; -1: must not go up), for any row `applies` to."""

    description: str
    applies: Callable[[pd.DataFrame], pd.Series]
    change: Callable[[pd.DataFrame], pd.DataFrame]
    direction: int = 1


def scores(actual: np.ndarray, p: pd.DataFrame) -> dict[str, float]:
    return score(actual, p["p10"].to_numpy(), p["p50"].to_numpy(), p["p90"].to_numpy())


def loads_and_reproduces(recomputed_mae: float, logged_mae: float) -> Gate:
    ok = abs(recomputed_mae - logged_mae) <= 0.01
    return Gate(
        "loads_and_reproduces",
        ok,
        f"P50 error {recomputed_mae:.3f} min from the registry vs {logged_mae:.3f} at training",
    )


def beats_baselines(metrics: dict[str, float]) -> Gate:
    """From the metrics logged at training: test_* vs every baseline_*_ on the same rows."""
    baselines = sorted({k.split("_mae")[0] for k in metrics if k.startswith("baseline_")
                        and k.endswith("_mae")})  # fmt: skip
    if not baselines:
        return Gate("beats_baselines", False, "no baseline scores were logged")
    worse = [
        b
        for b in baselines
        if metrics["test_mae"] >= metrics[f"{b}_mae"]
        or metrics["test_pinball"] >= metrics[f"{b}_pinball"]
    ]
    detail = ", ".join(
        f"{b.removeprefix('baseline_')} {metrics[f'{b}_mae']:.2f}" for b in baselines
    )
    return Gate(
        "beats_baselines",
        not worse,
        f"P50 error {metrics['test_mae']:.2f} min vs {detail}"
        + (f"; not beaten: {', '.join(worse)}" if worse else ""),
    )


def coverage(actual: np.ndarray, p: pd.DataFrame) -> Gate:
    c = scores(actual, p)["coverage"]
    low, high = COVERAGE_RANGE
    return Gate(
        "coverage", low <= c <= high, f"{c:.1%} inside P10-P90 (needs {low:.0%}-{high:.0%})"
    )


def beats_champion(actual: np.ndarray, p: pd.DataFrame, champion: pd.DataFrame | None) -> Gate:
    if champion is None:
        return Gate("beats_champion", True, "no champion yet")
    mine, theirs = scores(actual, p), scores(actual, champion)
    return Gate(
        "beats_champion",
        mine["pinball"] <= theirs["pinball"],
        f"pinball {mine['pinball']:.3f} vs champion {theirs['pinball']:.3f}; P50 error "
        f"{mine['mae']:.2f} vs {theirs['mae']:.2f} min",
    )


def no_segment_worse(
    rows: pd.DataFrame,
    actual: np.ndarray,
    p50: np.ndarray,
    champion_p50: np.ndarray | None,
    keys: list[str],
) -> Gate:
    if champion_p50 is None:
        return Gate("no_segment_worse", True, "no champion yet")
    mine, theirs = np.abs(actual - p50), np.abs(actual - champion_p50)
    worst: tuple[float, str] = (-np.inf, "")
    failures = []
    for key in keys:
        values = rows[key].astype(str).to_numpy()
        for value in pd.Series(values).value_counts().head(SEGMENT_TOP).index:
            mask = values == value
            gap = float(mine[mask].mean() - theirs[mask].mean())
            worst = max(worst, (gap, f"{key} {value}"))
            if gap > SEGMENT_MAX_WORSE_MIN:
                failures.append(f"{key} {value} +{gap:.1f}")
    return Gate(
        "no_segment_worse",
        not failures,
        f"worst top-{SEGMENT_TOP} {'/'.join(keys)}: {worst[1]} {worst[0]:+.2f} min"
        + (f"; over {SEGMENT_MAX_WORSE_MIN:.0f}: {', '.join(failures)}" if failures else ""),
    )


def behaviour(predict: Predict, rows: pd.DataFrame, rules: list[Behaviour]) -> list[Gate]:
    """Every rule must hold for every row it applies to (tolerance: rounding only)."""
    gates = []
    for rule in rules:
        subset = rows[rule.applies(rows).fillna(False).to_numpy(dtype=bool)]
        if subset.empty:
            gates.append(Gate(f"behaviour: {rule.description}", False, "no rows to test"))
            continue
        before = predict(subset)["p50"].to_numpy()
        after = predict(rule.change(subset.copy()))["p50"].to_numpy()
        wrong = rule.direction * (after - before) < -1e-6
        gates.append(
            Gate(
                f"behaviour: {rule.description}",
                not wrong.any(),
                f"{int(wrong.sum())} of {len(subset)} rows moved the wrong way"
                + (
                    f" (worst {rule.direction * (after - before).min():+.1f} min)"
                    if wrong.any()
                    else f"; average change {(after - before).mean():+.1f} min"
                ),  # fmt: skip
            )
        )
    return gates


def signature(expected_inputs: dict[str, str], logged_inputs: dict[str, str] | None) -> Gate:
    """`expected_inputs`: column -> type the live code sends ('string' or 'double')."""
    if logged_inputs is None:
        return Gate("signature", False, "the model has no input signature")
    missing = sorted(set(expected_inputs) - set(logged_inputs))
    extra = sorted(set(logged_inputs) - set(expected_inputs))
    mistyped = sorted(c for c in expected_inputs.keys() & logged_inputs.keys()
                      if expected_inputs[c] != logged_inputs[c])  # fmt: skip
    ok = not (missing or extra or mistyped)
    detail = f"{len(logged_inputs)} inputs match" if ok else (
        f"missing {missing}, unexpected {extra}, wrong type {mistyped}"
    )  # fmt: skip
    return Gate("signature", ok, detail)


def speed(predict: Predict, rows: pd.DataFrame) -> Gate:
    batch = rows.sample(SPEED_ROWS, replace=len(rows) < SPEED_ROWS, random_state=7)
    started = time.monotonic()
    predict(batch)
    took = time.monotonic() - started
    return Gate(
        "speed",
        took < SPEED_LIMIT_S,
        f"{len(batch):,} rows in {took:.1f} s (limit {SPEED_LIMIT_S:.0f} s)",
    )
