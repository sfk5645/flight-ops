"""DuckDB connections configured the same way everywhere."""

import duckdb


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the session time zone pinned to UTC.

    TIMESTAMPTZ values are formatted in the session time zone, so pinning it keeps
    output identical no matter which machine (laptop or GitHub runner) runs the job.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con
