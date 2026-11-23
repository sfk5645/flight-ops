import numpy as np
import pandas as pd

from flightops.models import gates


def ranges(p50: np.ndarray, half_width: float) -> pd.DataFrame:
    return pd.DataFrame({"p10": p50 - half_width, "p50": p50, "p90": p50 + half_width})


def test_coverage_must_be_between_75_and_85_percent() -> None:
    rng = np.random.default_rng(7)
    actual = rng.normal(0, 10, 10_000)
    p50 = np.zeros_like(actual)
    assert gates.coverage(actual, ranges(p50, 12.8)).passed  # ~80% of a normal(0, 10)
    assert not gates.coverage(actual, ranges(p50, 5)).passed  # far too narrow
    assert not gates.coverage(actual, ranges(p50, 30)).passed  # uselessly wide


def test_baselines_must_be_beaten_on_error_and_pinball() -> None:
    metrics = {
        "test_mae": 6.0, "test_pinball": 2.0,
        "baseline_a_mae": 17.0, "baseline_a_pinball": 7.0,
        "baseline_b_mae": 34.0, "baseline_b_pinball": 14.0,
    }  # fmt: skip
    assert gates.beats_baselines(metrics).passed
    assert not gates.beats_baselines(metrics | {"baseline_a_pinball": 1.9}).passed


def test_champion_gates_pass_without_a_champion_and_compare_with_one() -> None:
    actual = np.array([10.0, 20.0, 30.0, 40.0])
    good, bad = ranges(actual, 2), ranges(actual + 10, 2)
    assert gates.beats_champion(actual, good, None).passed
    assert gates.beats_champion(actual, good, bad).passed
    assert not gates.beats_champion(actual, bad, good).passed


def test_a_big_airport_much_worse_than_the_champion_fails() -> None:
    rows = pd.DataFrame({"dest": ["ATL"] * 50 + ["LGA"] * 50})
    actual = np.zeros(100)
    champion = np.zeros(100)
    mine = np.where(rows["dest"] == "LGA", 3.0, -0.5)  # LGA 3 minutes worse
    gate = gates.no_segment_worse(rows, actual, mine, champion, ["dest"])
    assert not gate.passed and "LGA" in gate.detail
    assert gates.no_segment_worse(rows, actual, np.full(100, 1.0), champion, ["dest"]).passed


def test_behaviour_fails_if_any_row_moves_the_wrong_way() -> None:
    rows = pd.DataFrame({"storm": [0.0, 0.0, 0.0], "x": [1.0, 2.0, 3.0]})
    rule = gates.Behaviour(
        "storm never earlier",
        lambda r: r["storm"] == 0,
        lambda r: r.assign(storm=1.0),
    )

    def sensible(r: pd.DataFrame) -> pd.DataFrame:
        return ranges((r["x"] + 5 * r["storm"]).to_numpy(), 1)

    def backwards_for_one(r: pd.DataFrame) -> pd.DataFrame:
        return ranges((r["x"] + np.where(r["x"] == 2, -1, 5) * r["storm"]).to_numpy(), 1)

    (ok,) = gates.behaviour(sensible, rows, [rule])
    (bad,) = gates.behaviour(backwards_for_one, rows, [rule])
    assert ok.passed
    assert not bad.passed and bad.detail.startswith("1 of 3")


def test_signature_must_match_what_the_code_sends() -> None:
    expected = {"dest": "string", "x": "double"}
    assert gates.signature(expected, {"dest": "string", "x": "double"}).passed
    assert not gates.signature(expected, {"dest": "string"}).passed
    assert not gates.signature(expected, {"dest": "double", "x": "double"}).passed
    assert not gates.signature(expected, None).passed


def test_loaded_model_must_reproduce_its_training_score() -> None:
    assert gates.loads_and_reproduces(6.312, 6.31).passed
    assert not gates.loads_and_reproduces(6.5, 6.31).passed
