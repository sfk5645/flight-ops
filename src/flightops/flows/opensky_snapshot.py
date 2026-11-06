"""Store one OpenSky snapshot of every aircraft in bronze.

    uv run --env-file .env python -m flightops.flows.opensky_snapshot

Meant to run every 5 minutes (GitHub Actions cron). Safe to re-run: a snapshot time that is
already stored is skipped.
"""

import logging

from flightops.config import OpenSkySettings
from flightops.ingestion.opensky import OpenSkyClient, ingest_snapshot
from flightops.storage import LakeStorage


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest_snapshot(LakeStorage.from_settings(), OpenSkyClient(OpenSkySettings()))


if __name__ == "__main__":
    main()
