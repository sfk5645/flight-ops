# flight-ops

A side project for predicting US flight delays and spotting weird flight behavior from live ADS-B data.

Before takeoff it predicts how late a flight will arrive, given the scheduled times from your booking. Once a plane is in the air, it predicts when it will land. Predictions are ranges instead of single numbers, since "probably 10–40 min late" is more useful than "23 min late". Historical data comes from the BTS on-time reports, and everything is stored as Parquet in Cloudflare R2.

Right now the historical pipeline is done: about three years of flights loaded and cleaned, plus an inferred timetable and each plane's sequence of flights. Live tracking and the models come next. `docs/schedule_match_rate.md` explains why the app asks for scheduled times instead of guessing them.

## Setup

You'll need [uv](https://docs.astral.sh/uv/) and an R2 bucket. Bucket setup is in `docs/setup.md`, and the keys go in a `.env` file.

```bash
uv sync
uv run pytest
```

## Loading the data

```bash
uv run --env-file .env python -m flightops.flows.bts_history --start 2023-09 --end 2026-08
uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt
```

The first command downloads the monthly BTS files and cleans them up. The second builds the tables the models will use and runs the data tests. Both are fine to re-run.

## Layout

- `src/flightops/` - ingestion, cleaning, pipelines
- `dbt/` - final tables and data tests
- `tests/` - unit and integration tests
- `docs/` - setup and findings
