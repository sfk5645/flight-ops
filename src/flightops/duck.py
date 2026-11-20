"""DuckDB connections configured the same way everywhere."""

import duckdb

from flightops.config import R2Settings


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the session time zone pinned to UTC.

    TIMESTAMPTZ values are formatted in the session time zone, so pinning it keeps
    output identical no matter which machine (laptop or GitHub runner) runs the job.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con


def connect_lake(settings: R2Settings | None = None) -> duckdb.DuckDBPyConnection:
    """connect(), plus access to the R2 bucket: read with lake_glob(...) paths."""
    settings = settings or R2Settings()
    con = connect()
    con.execute(
        "CREATE SECRET (TYPE r2, KEY_ID $key, SECRET $secret, ACCOUNT_ID $account)",
        {
            "key": settings.access_key_id,
            "secret": settings.secret_access_key.get_secret_value(),
            "account": settings.account_id,
        },
    )
    return con


def lake_glob(layer: str, dataset: str, settings: R2Settings | None = None) -> str:
    """All Parquet files of a dataset, e.g. r2://flight-ops-lake/gold/training_set/**/*.parquet"""
    settings = settings or R2Settings()
    return f"r2://{settings.bucket}/{layer}/{dataset}/**/*.parquet"
