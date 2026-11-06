"""Streaming HTTP downloads with retries."""

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "flight-ops/0.1 (+https://github.com/sfk5645/flight-ops)"


def download(url: str, dest: Path, *, attempts: int = 3, timeout: float = 300.0) -> Path:
    """Stream url to dest, retrying network errors and 5xx responses with backoff."""
    for attempt in range(1, attempts + 1):
        try:
            with httpx.stream(
                "GET",
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                with dest.open("wb") as file:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        file.write(chunk)
            return dest
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            is_client_error = (
                isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500
            )
            if is_client_error or attempt == attempts:
                raise
            wait = 2**attempt
            log.warning("download failed (%s), retrying in %ss: %s", error, wait, url)
            time.sleep(wait)
    raise AssertionError("unreachable")
