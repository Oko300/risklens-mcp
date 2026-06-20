"""
server.py
==========
RiskLens MCP — a focused MCP server exposing exactly two tools:

  1. analyze_8k_events       - risk analysis of a company's recent 8-K filings
  2. analyze_insider_activity - risk analysis of a company's recent Form 4 insider activity

Both tools pull live data from SEC EDGAR and are backed by a shared Upstash
Redis cache (3-day TTL) implemented in core/cache.py.

Transport: streamable-http, stateless. This server is designed to be
deployed once (e.g. on Render) and called concurrently by many different
clients over HTTP — it is NOT meant to be run as a local stdio MCP server
for a single desktop client.

Run locally:
    python server.py

Run on Render (or any host providing $PORT):
    The server automatically binds to 0.0.0.0:$PORT when PORT is set,
    falling back to 0.0.0.0:8000 otherwise.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("risklens.server")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from tools.eight_k_events import analyze_8k_events as _analyze_8k_events  # noqa: E402
from tools.insider_activity import analyze_insider_activity as _analyze_insider_activity  # noqa: E402

# ---------------------------------------------------------------------------
# Startup sanity checks — fail loudly and early rather than mysteriously
# later, since this runs unattended on a hosting platform.
# ---------------------------------------------------------------------------


def _check_required_env() -> None:
    missing = []
    if not os.getenv("SEC_EDGAR_USER_AGENT"):
        missing.append("SEC_EDGAR_USER_AGENT")
    # Redis is soft-required: cache.py degrades gracefully without it, but
    # we still want a loud warning on startup since "caching just doesn't
    # work" was the exact failure mode we're trying to avoid this time.
    if not os.getenv("UPSTASH_REDIS_REST_URL") or not os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        logger.warning(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set. "
            "The server will still work, but every call will hit SEC EDGAR live "
            "with no caching. Set these for production use."
        )
    if missing:
        logger.error(
            "Missing required environment variable(s): %s. "
            "See .env.example. The server will start, but tool calls will fail "
            "until this is set.",
            ", ".join(missing),
        )


_check_required_env()

# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="RiskLens MCP",
    instructions=(
        "RiskLens MCP provides two focused risk-analysis tools backed by live "
        "SEC EDGAR data: analyze_8k_events (8-K filing risk analysis) and "
        "analyze_insider_activity (Form 4 insider transaction risk analysis). "
        "Both tools default to a risk-focused read but support mode='summary' for "
        "a neutral, non-judgmental view of the same underlying filings. "
        "Results are cached for 3 days for speed; responses include "
        "'from_cache' so callers know whether data was freshly fetched."
    ),
    # Stateless HTTP: no session affinity required between requests, which
    # is exactly right for a multi-tenant deployment behind a single Render
    # URL. Combined with FastMCP's async tool handlers, this lets the
    # server process many different clients' calls concurrently.
    stateless_http=True,
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
)


@mcp.tool(
    name="analyze_8k_events",
    description=(
        "Analyze a company's recent SEC Form 8-K filings for risk signal. "
        "Surfaces and scores high-risk disclosure categories (restatements, "
        "bankruptcy/receivership, delisting notices, accelerated debt obligations, "
        "impairments, leadership departures, auditor changes) drawn from the "
        "filing's official item codes. Pass mode='summary' for a neutral list "
        "of recent 8-K filings without risk framing. Works for any US public "
        "company that files with the SEC."
    ),
)
async def analyze_8k_events(
    ticker: str,
    lookback_days: int = 180,
    mode: str = "risk",
) -> dict:
    """
    Args:
        ticker: Stock ticker symbol (e.g. "AAPL", "TSLA", "GME").
        lookback_days: How many days back to search (default 180, max 1825).
        mode: "risk" (default, risk-scored analysis) or "summary" (neutral filing list).
    """
    mode_normalized = mode if mode in ("risk", "summary") else "risk"
    return await _analyze_8k_events(ticker=ticker, lookback_days=lookback_days, mode=mode_normalized)


@mcp.tool(
    name="analyze_insider_activity",
    description=(
        "Analyze a company's recent SEC Form 4 insider transaction filings for risk "
        "and conviction signal. Parses actual transaction-level XML to detect clusters "
        "of open-market insider selling (especially by officers/directors), offsets "
        "this against open-market insider buying, and produces a risk score and "
        "plain-language narrative. Pass mode='summary' for a neutral transaction-by-"
        "transaction list without risk framing. Works for any US public company that "
        "files with the SEC."
    ),
)
async def analyze_insider_activity(
    ticker: str,
    lookback_days: int = 90,
    mode: str = "risk",
    max_filings: int = 25,
) -> dict:
    """
    Args:
        ticker: Stock ticker symbol (e.g. "AAPL", "TSLA", "GME").
        lookback_days: How many days back to search (default 90, max 730).
        mode: "risk" (default, risk-scored analysis) or "summary" (neutral transaction list).
        max_filings: Max number of individual Form 4 filings to fetch and parse (default 25, max 40).
    """
    mode_normalized = mode if mode in ("risk", "summary") else "risk"
    return await _analyze_insider_activity(
        ticker=ticker, lookback_days=lookback_days, mode=mode_normalized, max_filings=max_filings
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting RiskLens MCP on 0.0.0.0:%d (transport=streamable-http, stateless=True)", port)
    mcp.run(transport="streamable-http")