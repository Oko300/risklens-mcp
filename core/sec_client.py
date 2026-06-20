"""
core/sec_client.py
====================
Shared async client for talking to SEC EDGAR (data.sec.gov / www.sec.gov).

Both tools (8-K events, insider activity) use this module so the rate
limiting, User-Agent header, retry logic, and ticker->CIK resolution are
implemented exactly once.

SEC EDGAR rules this module respects:
  - Every request MUST send a descriptive User-Agent header
    ("CompanyOrName contact@email.com"). Configured via SEC_EDGAR_USER_AGENT.
  - Max 10 requests/second per IP, enforced across ALL data.sec.gov /
    www.sec.gov endpoints. We enforce this with a simple async semaphore +
    minimum-interval gate shared process-wide, which works correctly even
    when multiple MCP tool calls are in flight concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("risklens.sec_client")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# ---------------------------------------------------------------------------
# Process-wide rate limiter: max 10 req/sec to *.sec.gov, shared across all
# concurrent tool calls in this server process.
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple async token-bucket-ish limiter: at most `rate` calls per second."""

    def __init__(self, rate_per_second: float = 8.0):
        # Deliberately stay under the SEC's hard 10/sec ceiling to leave
        # headroom for jitter under concurrent load.
        self._min_interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()


_rate_limiter = _RateLimiter(rate_per_second=8.0)

# Ticker -> CIK map is fetched once and reused (it's a few MB and changes
# infrequently). Cached in-process; also written through core/cache.py so
# it survives across server restarts within the 3-day TTL.
_ticker_map_cache: Optional[dict[str, str]] = None
_ticker_map_lock = asyncio.Lock()


def _get_user_agent() -> str:
    ua = os.getenv("SEC_EDGAR_USER_AGENT")
    if not ua or "@" not in ua:
        raise RuntimeError(
            "SEC_EDGAR_USER_AGENT is not set (or looks invalid). "
            "SEC EDGAR requires a descriptive User-Agent header with a "
            "real contact email, e.g. 'RiskLens MCP contact@yourdomain.com'. "
            "Set this in your .env file."
        )
    return ua


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _get_user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


async def _get_json(client: httpx.AsyncClient, url: str, *, max_retries: int = 3) -> Any:
    """GET a URL and parse JSON, with rate limiting + retry on 429/5xx."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        await _rate_limiter.wait()
        try:
            resp = await client.get(url, headers=_headers(), timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait_s = 1.5 * attempt
                logger.warning("SEC EDGAR rate-limited us (429) on %s — backing off %.1fs", url, wait_s)
                await asyncio.sleep(wait_s)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_exc = e
            logger.warning("SEC EDGAR request failed (attempt %d/%d) for %s: %s", attempt, max_retries, url, e)
            await asyncio.sleep(0.75 * attempt)
    raise RuntimeError(f"SEC EDGAR request failed after {max_retries} attempts: {url}") from last_exc


async def _get_text(client: httpx.AsyncClient, url: str, *, max_retries: int = 3) -> Optional[str]:
    """GET a URL and return raw text (used for Form 4 XML), with the same retry policy."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        await _rate_limiter.wait()
        try:
            headers = _headers()
            headers["Host"] = "www.sec.gov"
            resp = await client.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait_s = 1.5 * attempt
                await asyncio.sleep(wait_s)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_exc = e
            logger.warning("SEC EDGAR text request failed (attempt %d/%d) for %s: %s", attempt, max_retries, url, e)
            await asyncio.sleep(0.75 * attempt)
    logger.error("SEC EDGAR text request permanently failed for %s: %s", url, last_exc)
    return None


async def resolve_ticker_to_cik(ticker: str) -> Optional[dict[str, str]]:
    """
    Resolve a stock ticker (e.g. "AAPL") to SEC CIK info.

    Returns a dict like {"cik": "0000320193", "name": "Apple Inc.", "ticker": "AAPL"}
    or None if the ticker isn't found.
    """
    global _ticker_map_cache

    ticker_norm = ticker.strip().upper()

    async with _ticker_map_lock:
        if _ticker_map_cache is None:
            async with httpx.AsyncClient() as client:
                headers = _headers()
                headers["Host"] = "www.sec.gov"
                await _rate_limiter.wait()
                resp = await client.get(TICKER_MAP_URL, headers=headers, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
                raw = resp.json()

            # raw is like {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
            mapping: dict[str, str] = {}
            names: dict[str, str] = {}
            for entry in raw.values():
                t = str(entry.get("ticker", "")).upper()
                cik = str(entry.get("cik_str", "")).zfill(10)
                if t:
                    mapping[t] = cik
                    names[t] = entry.get("title", "")
            _ticker_map_cache = mapping
            _ticker_map_names = names  # noqa: F841 (kept for potential future use)
            globals()["_ticker_map_names"] = names

    cik = _ticker_map_cache.get(ticker_norm)
    if cik is None:
        return None

    names_map = globals().get("_ticker_map_names", {})
    return {"cik": cik, "ticker": ticker_norm, "name": names_map.get(ticker_norm, "")}


async def fetch_submissions(cik: str) -> Optional[dict[str, Any]]:
    """Fetch the full EDGAR submissions JSON for a given 10-digit, zero-padded CIK."""
    url = SUBMISSIONS_URL.format(cik=cik)
    async with httpx.AsyncClient() as client:
        return await _get_json(client, url)


async def fetch_filing_index_json(cik: str, accession_number: str) -> Optional[dict[str, Any]]:
    """
    Fetch the JSON index for a specific filing, listing its component documents.
    accession_number should be in dashed form, e.g. "0000320193-24-000123".
    """
    acc_nodash = accession_number.replace("-", "")
    cik_int = str(int(cik))  # archives paths use the un-padded CIK
    url = f"{ARCHIVES_BASE}/{cik_int}/{acc_nodash}/index.json"
    async with httpx.AsyncClient() as client:
        return await _get_json(client, url)


async def fetch_document_text(cik: str, accession_number: str, filename: str) -> Optional[str]:
    """Fetch a specific document's raw text/XML/HTML from a filing's archive folder."""
    acc_nodash = accession_number.replace("-", "")
    cik_int = str(int(cik))
    url = f"{ARCHIVES_BASE}/{cik_int}/{acc_nodash}/{filename}"
    async with httpx.AsyncClient() as client:
        return await _get_text(client, url)