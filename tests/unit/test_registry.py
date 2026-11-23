import mlflow
import numpy as np
import pandas as pd
import pytest

from flightops.models import registry
from flightops.models.delay import predep

LABEL = "y"


@pytest.fixture
def local_mlflow(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    # a throwaway file-based tracking server and registry, never DagsHub
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tmp_path.as_uri())
    mlflow.set_tracking_uri(tmp_path.as_uri())
    yield
    mlflow.set_tracking_uri(None)


def frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = pd.DataFrame(
        {
            "x": rng.uniform(0, 100, n),
            "airport": rng.choice(["ATL", "LGA", "DEN"], n),
        }
    )
    rows[LABEL] = rows["x"] + rows["airport"].map({"ATL": 0, "LGA": 20, "DEN": 40})
    rows[LABEL] += rng.normal(0, 3, n)
    rows["airport"] = rows["airport"].astype("category")
    return rows


def test_a_registered_model_loads_back_and_predicts_the_same(local_mlflow: None) -> None:
    train, valid, test = frame(3000, 1), frame(2000, 2), frame(500, 3)
    model = predep.train(
        train, valid, ["x", "airport"], label=LABEL, categorical=["airport"],
        calibration_bands=[50.0],
    )  # fmt: skip
    version = registry.register(
        model,
        name="test-model",
        categorical=["airport"],
        sample_rows=train,
        metrics={"test_mae": 1.0},
        params={"train_rows": len(train)},
    )
    assert version == "1"

    loaded = mlflow.pyfunc.load_model("models:/test-model/1")
    # callers send plain text and numbers, as the signature says
    sent = test[["x", "airport"]].astype({"airport": str})
    expected = model.predict(test)
    got = loaded.predict(sent)
    np.testing.assert_allclose(got[["p10", "p50", "p90"]], expected[["p10", "p50", "p90"]])

    run = mlflow.get_run(mlflow.MlflowClient().get_model_version("test-model", "1").run_id)
    assert run.data.metrics["test_mae"] == 1.0
    assert run.data.params["train_rows"] == "3000"
    assert "git_commit" in run.data.tags
