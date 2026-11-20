import numpy as np
import pandas as pd

from flightops.models.delay import baselines, predep
from flightops.models.delay.features import LABEL, prepare, split_by_time
from flightops.models.delay.metrics import score


def test_score() -> None:
    actual = np.array([0.0, 10.0, 100.0])
    s = score(actual, p10=np.full(3, -5.0), p50=np.full(3, 5.0), p90=np.full(3, 20.0))
    assert s["mae"] == (5 + 5 + 95) / 3
    assert s["within_15"] == 2 / 3
    assert s["coverage"] == 2 / 3  # 100 is outside -5..20
    assert s["width"] == 25


def months_frame(months: int) -> pd.DataFrame:
    rows = [
        {"year": 2025 + (m // 12), "month": m % 12 + 1, "carrier": "DL", "origin": "ATL",
         "dest": "LGA", "inbound_status": "landed", "origin_thunderstorm": False, LABEL: m}
        for m in range(months)
    ]  # fmt: skip
    frame = pd.DataFrame(rows)
    frame["origin_thunderstorm"] = frame["origin_thunderstorm"].astype("boolean")
    return prepare(frame)


def test_split_is_by_time_newest_last() -> None:
    split = split_by_time(months_frame(20))
    assert split.train[LABEL].max() < split.valid[LABEL].min()
    assert split.valid[LABEL].max() < split.test[LABEL].min()
    assert len(split.test) == 3 and len(split.valid) == 3 and len(split.train) == 14


def test_prepare_types() -> None:
    frame = months_frame(2)
    assert frame["carrier"].dtype == "category"
    assert frame["origin_thunderstorm"].dtype == "float32"


def test_route_hour_baseline_falls_back_when_few_flights() -> None:
    train = pd.DataFrame(
        {"origin": ["ATL"] * 40, "dest": ["LGA"] * 40, "sched_dep_hour_local": [8] * 40,
         LABEL: range(40)}
    )  # fmt: skip
    rows = pd.DataFrame(
        {"origin": ["ATL", "ATL", "BOS"], "dest": ["LGA", "LGA", "SFO"],
         "sched_dep_hour_local": [8, 9, 8]}
    )  # fmt: skip
    p = baselines.route_hour(train, rows)
    assert p["p50"].tolist() == [19.5, 19.5, 19.5]  # exact, route-level, overall
    assert p.loc[0, "p10"] < p.loc[0, "p50"] < p.loc[0, "p90"]


def test_quantile_model_learns_the_spread_and_keeps_order() -> None:
    rng = np.random.default_rng(7)
    n = 4000
    frame = pd.DataFrame({"x": rng.uniform(0, 1, n)})
    frame[LABEL] = 30 * frame["x"] + rng.normal(0, 10, n)
    model = predep.train(frame.iloc[:3000], frame.iloc[3000:], ["x"])
    p = model.predict(frame.iloc[3000:])
    assert (p["p10"] <= p["p50"]).all() and (p["p50"] <= p["p90"]).all()
    inside = (
        (frame[LABEL].iloc[3000:] >= p["p10"]) & (frame[LABEL].iloc[3000:] <= p["p90"])
    ).mean()
    assert 0.7 < inside < 0.9


def test_breakdown_shares_add_up_and_concentration_is_sorted() -> None:
    from flightops.models.delay import breakdown

    rows = pd.DataFrame(
        {
            LABEL: [0.0, 10.0, 200.0, -20.0],
            "inbound_status": pd.Categorical(["landed", "airborne", None, "landed"]),
            "inbound_days_same": [7, 1, None, 5],
            "inbound_buffer_min": [90.0, -10.0, None, 40.0],
            "sched_dep_hour_local": [6, 13, 18, 22],
            "origin_thunderstorm": [0.0, 0.0, 1.0, 0.0],
            "dest_thunderstorm": [0.0, 0.0, 0.0, 0.0],
            "origin_flight_category": pd.array([0, 2, None, 0], dtype="Int8"),
            "dest_flight_category": [0, 0, 0, 0],
            "origin_snow_or_ice": [0.0, 0.0, 0.0, 0.0],
            "dest_snow_or_ice": [0.0, 0.0, 0.0, 0.0],
            "flight_date": pd.to_datetime(["2026-06-01"] * 2 + ["2026-07-01"] * 2),
            "carrier": pd.Categorical(["DL", "DL", "UA", "WN"]),
        }
    )
    p50 = np.array([0.0, 0.0, 20.0, 0.0])  # errors 0, 10, 180, 20 -> total 210
    tables = breakdown.breakdown(rows, p50, baseline_p50=np.zeros(4))
    weather = tables["weather at either end"].set_index("weather at either end")
    assert weather.loc["thunderstorm", "error share"] == f"{180 / 210:.1%}"
    assert weather.loc["IFR or LIFR", "model error"] == 10
    assert tables["inbound status"].set_index("inbound status").loc["no guess", "bias"] == 180
    worst = breakdown.concentration(rows[LABEL].to_numpy(), p50)
    assert worst.loc[0, "smallest error in group (min)"] == 180
