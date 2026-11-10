"""Turn one day's OpenSky snapshots into silver/tracked_flights (see flightops.tracking.daily).

    uv run --env-file .env python -m flightops.flows.compact_snapshots              # yesterday
    uv run --env-file .env python -m flightops.flows.compact_snapshots --date 2026-11-09

Runs every evening (UTC) for the previous day. Safe to re-run: the day's file is rebuilt and
replaced.
"""

import argparse
import logging
from datetime import UTC, date, datetime, timedelta

from flightops.storage import LakeStorage
from flightops.tracking.daily import compact_day


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, help="UTC day (default: yesterday)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = datetime.now(UTC).date()
    day = args.date or today - timedelta(days=1)
    if day >= today:
        parser.error(f"{day} isn't over yet")
    compact_day(LakeStorage.from_settings(), day)


if __name__ == "__main__":
    main()
