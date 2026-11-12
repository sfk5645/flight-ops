"""Airport weather history: a month of hourly reports (METARs) from the Iowa Environmental
Mesonet (IEM) archive, stored unchanged in bronze.

IEM's data is public domain ("may be used freely by anyone for any lawful purpose"; credit
appreciated). Its download service throttles each IP to one request per second and caps a request
at 1,000 station-years, so one request per month for all ~350 airports (~30 station-years) is
both the politest and the fastest way to fetch it.

IEM decodes each report into columns; we keep only the ones the weather flags need:
visibility, the lowest three cloud layers, present-weather codes (TS thunderstorm, SN snow,
FZRA freezing rain, ...), wind and gusts. The live feed (aviationweather.gov) carries the same
reports, so the flags can be computed the same way in training and live.
"""

import calendar
import io
import logging
import time
from datetime import date, timedelta

import httpx
import pyarrow as pa
import pyarrow.csv as pacsv

from flightops.ingestion.download import USER_AGENT
from flightops.storage import LakeStorage, lake_key

log = logging.getLogger(__name__)

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
FIELDS = [
    "vsby", "skyc1", "skyc2", "skyc3", "skyl1", "skyl2", "skyl3", "wxcodes", "sknt", "gust",
]  # fmt: skip
ROUTINE_AND_SPECIALS = "3,4"  # hourly reports plus the extra ones issued when weather changes
SETTLE_DAYS = 2  # late reports keep arriving in the archive for a day or so after the fact


def bronze_key(year: int, month: int) -> str:
    return lake_key("bronze", "iem_metar", year=str(year), month=f"{month:02d}")


def iem_station(iata: str, icao: str | None) -> str:
    """IEM's id for an airport: the ICAO code, minus the K that every lower-48 code starts with
    (KORD -> ORD, PHNL stays PHNL). Airports without an ICAO code fall back to the IATA code."""
    if not icao:
        return iata
    return icao[1:] if len(icao) == 4 and icao.startswith("K") else icao


def fetch_month(stations: list[str], year: int, month: int, *, attempts: int = 5) -> pa.Table:
    """Every report for `stations` in one UTC month, all columns as text (typed in silver)."""
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day) + timedelta(days=1)
    params = {
        "station": ",".join(sorted(stations)),
        "data": ",".join(FIELDS),
        "sts": f"{year}-{month:02d}-01T00:00Z",
        "ets": f"{end.isoformat()}T00:00Z",
        "tz": "UTC",
        "format": "onlycomma",
        "missing": "empty",
        "report_type": ROUTINE_AND_SPECIALS,
    }
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.get(
                IEM_URL, params=params, timeout=600, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
            return read_csv(response.content)
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            # 429 = too many requests, 503 = server busy: both say "try again later".
            retry = not isinstance(error, httpx.HTTPStatusError) or (
                error.response.status_code == 429 or error.response.status_code >= 500
            )
            if not retry or attempt == attempts:
                raise
            wait = 5 * 2**attempt
            log.warning("IEM request failed (%s), retrying in %ss", error, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def read_csv(content: bytes) -> pa.Table:
    columns = ["station", "valid", *FIELDS]
    return pacsv.read_csv(
        io.BytesIO(content),
        convert_options=pacsv.ConvertOptions(
            column_types=dict.fromkeys(columns, pa.string()),
            include_columns=columns,
            strings_can_be_null=False,
        ),
    )


def ingest_month(
    lake: LakeStorage, year: int, month: int, stations: list[str], *, today: date
) -> str:
    """Download one month into bronze. Skips months already there, so re-running is safe, and
    refuses months that haven't settled: bronze is never rewritten, so a month stored too early
    would stay incomplete."""
    key = bronze_key(year, month)
    if lake.exists(key):
        log.info("bronze %s already exists, skipping download", key)
        return key
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    if today <= last_day + timedelta(days=SETTLE_DAYS):
        raise ValueError(f"{year}-{month:02d} isn't complete yet; try after {SETTLE_DAYS} days")
    table = fetch_month(stations, year, month)
    lake.write_parquet(key, table)
    log.info("bronze %s: %s reports from %s stations", key, table.num_rows, len(stations))
    return key
