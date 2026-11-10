"""Rolling retention: keep only the most recent data in each dataset, so the lake stays a
fixed size (the R2 free tier is 10 GB, and nothing should grow forever).

Two kinds of rule:
- MONTHLY_RULES: keep the newest N months present (year=YYYY/month=MM partitions). Counted
  from the newest month in the dataset, not today, because BTS publishes ~2 months late.
- DAILY_RULES: keep the last N days before today (date=YYYY-MM-DD partitions).

Raw OpenSky snapshots (bronze/opensky_states/) are expired by an R2 lifecycle rule instead
(3 days, see docs/setup.md). Lifecycle rules count from upload time, which suits data written
once a day, but not BTS, whose months were all uploaded at once.

Only keys that sit inside a recognized partition folder can be deleted; anything else under a
prefix is left alone.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from flightops.storage import LakeStorage

log = logging.getLogger(__name__)

BTS_MONTHS = 24

MONTHLY_RULES: dict[str, int] = {
    "bronze/bts_ontime/": BTS_MONTHS,
    "silver/bts_flights/": BTS_MONTHS,
    "quarantine/bts_flights/": BTS_MONTHS,
    "gold/fct_rotations/": BTS_MONTHS,
}

# Datasets written daily by the live pipeline, added as they are built (Phase 3+).
DAILY_RULES: dict[str, int] = {
    "silver/tracked_flights/": 90,
}

_MONTH = re.compile(r"/year=(\d{4})/month=(\d{1,2})/[^/]+$")
_DAY = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/[^/]+$")


def expired_month_keys(keys: list[str], keep: int) -> list[str]:
    """Keys whose (year, month) is older than the newest `keep` months present."""
    months = {key: (int(m[1]), int(m[2])) for key in keys if (m := _MONTH.search(key))}
    newest_first = sorted(set(months.values()), reverse=True)
    if len(newest_first) <= keep:
        return []
    cutoff = newest_first[keep - 1]  # the oldest month we keep
    return sorted(key for key, month in months.items() if month < cutoff)


def expired_day_keys(keys: list[str], keep: int, today: date) -> list[str]:
    """Keys whose date is before the last `keep` days (today counts as one of them)."""
    first_kept = (today - timedelta(days=keep - 1)).isoformat()
    return sorted(key for key in keys if (m := _DAY.search(key)) and m[1] < first_kept)


def prune(lake: LakeStorage, today: date, *, dry_run: bool = False) -> list[str]:
    """Apply every retention rule. Returns the deleted (or, with dry_run, deletable) keys."""
    deleted: list[str] = []
    for prefix, keep in MONTHLY_RULES.items():
        deleted += _delete(lake, prefix, expired_month_keys(lake.list_keys(prefix), keep), dry_run)
    for prefix, keep in DAILY_RULES.items():
        expired = expired_day_keys(lake.list_keys(prefix), keep, today)
        deleted += _delete(lake, prefix, expired, dry_run)
    return deleted


def _delete(lake: LakeStorage, prefix: str, keys: list[str], dry_run: bool) -> list[str]:
    if keys:
        action = "would delete" if dry_run else "deleting"
        log.info("%s %d expired files under %s (oldest %s)", action, len(keys), prefix, keys[0])
        if not dry_run:
            lake.delete(keys)
    return keys
