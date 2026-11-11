# flight-ops

A side project for predicting US flight delays and spotting weird flight behavior from live ADS-B data.

Before takeoff it predicts how late a flight will arrive, given the scheduled times from your booking. Once a plane is in the air, it predicts when it will land. Predictions are ranges instead of single numbers, since "probably 10–40 min late" is more useful than "23 min late". Historical data comes from the BTS on-time reports, and everything is stored as Parquet in Cloudflare R2.

Right now the historical pipeline is done: the last two years of US flights loaded and cleaned, plus an inferred timetable and each plane's sequence of flights. Live tracking runs every 5 minutes and turns positions into flights with takeoff and landing airports and times; checked against BTS, it finds 90% of US airline flights and its landing times are within 2 minutes for 99% of them (`docs/landing_validation.md`). The models come next. `docs/schedule_match_rate.md` explains why the app asks for scheduled times instead of guessing them.

## Setup

You'll need [uv](https://docs.astral.sh/uv/) and an R2 bucket. Bucket setup is in `docs/setup.md`, and the keys go in a `.env` file.

```bash
uv sync
uv run pytest
```

## Loading the data

```bash
uv run --env-file .env python -m flightops.flows.bts_history --start 2024-09 --end 2026-08
uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt
```

The first command downloads the monthly BTS files and cleans them up. The second builds the tables the models will use and runs the data tests. Both are fine to re-run.

The lake only keeps a rolling window (24 months of BTS, a few days of raw positions), so it stays the same size over time. To clear out anything older:

```bash
uv run --env-file .env python -m flightops.flows.prune_lake --dry-run
uv run --env-file .env python -m flightops.flows.prune_lake
```

## Layout

- `src/flightops/` - ingestion, cleaning, pipelines
- `dbt/` - final tables and data tests
- `tests/` - unit and integration tests
- `docs/` - setup and findings

## Data sources

- US flight history: [BTS On-Time Performance](https://www.transtats.bts.gov/) (US Department of Transportation)
- Airports: [OurAirports](https://ourairports.com/data/); airlines: [OpenFlights](https://openflights.org/data)
- Live aircraft positions: [The OpenSky Network](https://opensky-network.org), used under its terms for non-profit research and education. Matthias Schäfer, Martin Strohmeier, Vincent Lenders, Ivan Martinovic and Matthias Wilhelm. "Bringing up OpenSky: A large-scale ADS-B sensor network for research". In *Proceedings of the 13th IEEE/ACM International Symposium on Information Processing in Sensor Networks (IPSN)*, pages 83–94, April 2014.
- Historical aircraft positions (landing validation): [adsb.lol](https://adsb.lol) daily history, [Open Database License](https://opendatacommons.org/licenses/odbl/1-0/)
