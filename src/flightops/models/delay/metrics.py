"""How good a range prediction is (P10, P50, P90 in minutes late)."""

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.floating]


def score(actual: Array, p10: Array, p50: Array, p90: Array) -> dict[str, float]:
    """
    mae            average minutes between the P50 and what happened (lower is better)
    within_15      share of flights where the P50 was within 15 minutes
    coverage       share that landed inside P10..P90 (target 80%: wider is not "better")
    width          median width of the range in minutes (narrower is better, at 80% coverage)
    pinball        quantile loss averaged over P10/P50/P90: one number that rewards both
                   accuracy and honest ranges; models are compared on it
    """
    error = actual - p50
    pinball = np.mean([_pinball(actual, p, q) for p, q in ((p10, 0.1), (p50, 0.5), (p90, 0.9))])
    return {
        "mae": float(np.mean(np.abs(error))),
        "within_15": float(np.mean(np.abs(error) <= 15)),
        "coverage": float(np.mean((actual >= p10) & (actual <= p90))),
        "width": float(np.median(p90 - p10)),
        "pinball": float(pinball),
    }


def _pinball(actual: Array, predicted: Array, quantile: float) -> float:
    diff = actual - predicted
    return float(np.mean(np.maximum(quantile * diff, (quantile - 1) * diff)))
