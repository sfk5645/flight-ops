"""Delete data older than each dataset's retention window (see flightops.retention).

    uv run --env-file .env python -m flightops.flows.prune_lake --dry-run   # show what would go
    uv run --env-file .env python -m flightops.flows.prune_lake

Safe to re-run: once old partitions are gone there is nothing left to delete. Afterwards it
checks the bucket's total size and fails (so GitHub emails the owner) if it is over 8 GB.
"""

import argparse
import logging
from datetime import UTC, datetime

from flightops.retention import check_size, prune
from flightops.storage import LakeStorage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="list expired files only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = datetime.now(UTC).date()
    lake = LakeStorage.from_settings()
    deleted = prune(lake, today, dry_run=args.dry_run)
    logging.info("%s %d files", "would delete" if args.dry_run else "deleted", len(deleted))
    check_size(lake)


if __name__ == "__main__":
    main()
