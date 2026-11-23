"""Save trained quantile models to the MLflow model registry (hosted on DagsHub).

Each call to `register` makes one MLflow run in the model's experiment and one new version of
its registered model:
- the model itself, as an MLflow "pyfunc" (load it with mlflow.pyfunc.load_model and call
  .predict on a table of inputs; it returns p10/p50/p90). Inside are the three LightGBM models
  as text files plus a small JSON with the input list, which inputs are categories, and the
  range calibration, so a loaded model predicts exactly what the trained one did;
- its signature (the input columns and types predict_live must send);
- metrics (test scores of the model and its baselines), parameters (data window, rows,
  settings) and the git commit it was trained from.

Which version is used live is decided by aliases (`champion`, `challenger`), set by the
promotion gates, never here. Connection settings come from MLFLOW_TRACKING_URI /
MLFLOW_TRACKING_USERNAME / MLFLOW_TRACKING_PASSWORD (.env locally, GitHub secrets in
workflows). The DagsHub project is public: nothing logged may contain aircraft identifiers
(OpenSky terms); models only know airline and airport codes.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import lightgbm as lgb
import pandas as pd

from flightops.models.delay.predep import QuantileModel

# Registered model names (also used as experiment names).
PREDEP = "predep-delay"
INFLIGHT = "inflight-landing"

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")


def _pyfunc_class() -> type:
    """The MLflow wrapper class, defined on first use so importing this module stays cheap."""
    from mlflow.pyfunc.model import PythonModel

    class _QuantilePyfunc(PythonModel):
        def load_context(self, context: Any) -> None:
            spec = json.loads(Path(context.artifacts["spec"]).read_text())
            boosters = {
                name: lgb.Booster(model_file=context.artifacts[name]) for name in spec["quantiles"]
            }
            self.categorical: list[str] = spec["categorical"]
            self.model = QuantileModel(
                spec["features"], boosters, spec["shifts"], spec["band_edges"]
            )

        def predict(self, context: Any, model_input: pd.DataFrame, params: Any = None) -> Any:
            return self.model.predict(as_model_input(model_input, self.categorical))

    return _QuantilePyfunc


def as_model_input(rows: pd.DataFrame, categorical: list[str]) -> pd.DataFrame:
    """Categories as pandas categories (LightGBM maps them to its training codes), every other
    input as float. The logged signature uses text and doubles, which is what callers send."""
    rows = rows.copy()
    for column in rows.columns:
        if column in categorical:
            rows[column] = rows[column].astype("string").astype("category")
        else:
            rows[column] = pd.to_numeric(rows[column], errors="coerce").astype("float64")
    return rows


def signature_example(rows: pd.DataFrame, categorical: list[str]) -> pd.DataFrame:
    """Inputs as callers send them: text categories, float numbers."""
    rows = as_model_input(rows, categorical)
    for column in categorical:
        if column in rows:
            rows[column] = rows[column].astype("string").astype(object)
    return rows


def git_commit() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}{'-dirty' if dirty else ''}"


def register(
    model: QuantileModel,
    *,
    name: str,
    categorical: list[str],
    sample_rows: pd.DataFrame,
    metrics: dict[str, float],
    params: dict[str, Any],
) -> str:
    """Log `model` as a new version of registered model `name`. Returns the version number."""
    import mlflow
    from mlflow.models import infer_signature

    features = model.features
    example = signature_example(sample_rows[features].head(1000), categorical)
    signature = infer_signature(example, model.predict(as_model_input(example, categorical)))

    mlflow.set_experiment(name)
    with tempfile.TemporaryDirectory() as tmp, mlflow.start_run() as run:
        artifacts = {}
        for quantile, booster in model.boosters.items():
            path = Path(tmp, f"{quantile}.txt")
            booster.save_model(str(path))
            artifacts[quantile] = str(path)
        spec = {
            "features": features,
            "categorical": [c for c in categorical if c in features],
            "quantiles": list(model.boosters),
            "shifts": model.shifts,
            "band_edges": model.band_edges,
        }
        Path(tmp, "spec.json").write_text(json.dumps(spec, indent=2))
        artifacts["spec"] = str(Path(tmp, "spec.json"))

        mlflow.set_tag("git_commit", git_commit())
        mlflow.log_params(
            {
                **params,
                "n_features": len(features),
                "trees": json.dumps({k: b.best_iteration for k, b in model.boosters.items()}),
            }
        )
        mlflow.log_metrics(metrics)
        mlflow.log_dict(spec, "spec.json")
        info = mlflow.pyfunc.log_model(
            name="model",
            python_model=_pyfunc_class()(),
            artifacts=artifacts,
            signature=signature,
            registered_model_name=name,
        )
    version = str(info.registered_model_version)
    mlflow.MlflowClient().set_model_version_tag(name, version, "run_id", run.info.run_id)
    return version


def score_metrics(prefix: str, scores: dict[str, float]) -> dict[str, float]:
    """metrics.score() output as MLflow metric names, e.g. test_mae."""
    return {f"{prefix}_{key}": round(value, 4) for key, value in scores.items()}
