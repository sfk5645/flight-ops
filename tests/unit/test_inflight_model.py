import numpy as np
import pandas as pd

from flightops.models.delay import inflight, predep
from flightops.models.delay.inflight import LABEL


def moments(days: int, per_day: int = 50, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * per_day
    remaining = rng.uniform(5, 200, n)
    frame = pd.DataFrame(
        {
            "date": np.repeat(pd.date_range("2026-10-22", periods=days), per_day),
            "direct_estimate_min": remaining * 0.8,  # planes slow down: distance/speed is short
            "route_remaining_min": np.where(rng.uniform(size=n) < 0.2, np.nan, remaining - 3),
            "x": remaining,
        }
    )
    frame[LABEL] = remaining + rng.normal(0, 2, n)
    return frame


def test_split_keeps_the_newest_days_for_testing() -> None:
    split = inflight.split_by_time(moments(20))
    assert split.train["date"].max() < split.valid["date"].min()
    assert split.valid["date"].max() < split.test["date"].min()
    assert split.test["date"].nunique() == inflight.TEST_DAYS
    assert split.valid["date"].nunique() == inflight.VALID_DAYS


def test_usual_remaining_falls_back_to_distance_over_speed_and_never_goes_negative() -> None:
    rows = pd.DataFrame(
        {"route_remaining_min": [30.0, -12.0, np.nan], "direct_estimate_min": [40.0, 5.0, 25.0]}
    )
    assert inflight.usual_remaining(rows).tolist() == [30.0, 0.0, 25.0]


def test_baseline_range_comes_from_its_own_training_misses() -> None:
    frame = moments(20)
    split = inflight.split_by_time(frame)
    p = inflight.with_range(split.train, split.test, inflight.direct)
    y = split.test[LABEL]
    # distance/speed is always short here, so the range must sit above the estimate
    assert (p["p10"] > p["p50"]).all()
    assert 0.7 < ((y >= p["p10"]) & (y <= p["p90"])).mean() < 0.9


def test_bands_cover_every_minute() -> None:
    bands = inflight.band(np.array([0, 14.9, 15, 59, 300]))
    assert bands.astype(str).tolist() == ["0-15", "0-15", "15-30", "30-60", "240+"]


def test_quantile_model_trains_on_another_label() -> None:
    split = inflight.split_by_time(moments(20, per_day=500))  # leaves need 500 rows
    model = predep.train(split.train, split.valid, ["x"], label=LABEL, categorical=[])
    error = (split.test[LABEL] - model.predict(split.test)["p50"]).abs().mean()
    assert error < 5


def test_banded_calibration_fits_each_band_separately() -> None:
    # misses grow with the time left (like real flights): short ones need a narrow range,
    # long ones a wide one; one shared shift would get both wrong
    rng = np.random.default_rng(7)

    def days(n: int) -> pd.DataFrame:
        x = rng.uniform(5, 200, n)
        return pd.DataFrame({"x": x, LABEL: x + rng.normal(0, 1 + x / 10, n)})

    calm = pd.DataFrame({"x": rng.uniform(5, 200, 6000)})
    calm[LABEL] = calm["x"]  # trained without noise: the raw range is far too narrow
    valid, test = days(20000), days(20000)

    def coverage_by_band(model: predep.QuantileModel) -> list[float]:
        p = model.predict(test)
        inside = (test[LABEL] >= p["p10"]) & (test[LABEL] <= p["p90"])
        return [float(inside[test["x"] < 30].mean()), float(inside[test["x"] > 120].mean())]

    shared = predep.train(calm, calm, ["x"], label=LABEL, categorical=[])
    shared.shifts = predep.calibrate(shared, valid, label=LABEL)
    near, far = coverage_by_band(shared)
    assert near > 0.95 and far < 0.7

    banded = predep.train(
        calm, calm, ["x"], label=LABEL, categorical=[], calibration_bands=[30.0, 60.0, 120.0]
    )
    banded.shifts = predep.calibrate(banded, valid, label=LABEL)
    assert len(banded.shifts["p10"]) == 4
    assert all(0.75 < c < 0.85 for c in coverage_by_band(banded))
