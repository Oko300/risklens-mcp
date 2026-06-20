"""
core/cache.py
==============
Centralized caching layer for RiskLens MCP, backed by Upstash Redis.

Design goals (this module exists specifically to avoid the problems from the
previous project: silent write/read mismatches, inconsistent keys, and tools
breaking when Redis has a hiccup):

1. ONE place builds cache keys. Tools never hand-roll a key string.
2. ONE place talks to Redis. Tools never import upstash_redis directly.
3. Every Redis call is wrapped in try/except. A Redis outage degrades to
   "no cache" (tool still works, just slower) — it never raises up into
   the tool and never breaks a request.
4. Values are always stored as JSON strings with a small envelope
   (cached_at, ttl_seconds, data) so we can tell, on read, when an entry
   was written and what's actually inside it — instead of guessing.
5. TTL is fixed at 3 days (259200 seconds) via a single constant, so it
   can't drift between call sites.

Usage from a tool:

    from core.cache import build_cache_key, get_cached, set_cached

    key = build_cache_key("8k_events", ticker="AAPL", lookback_days=90)
    cached = await get_cached(key)
    if cached is not None:
        return cached

    result = {...}  # do the real work
    await set_cached(key, result)
    return result
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

from upstash_redis.asyncio import Redis

logger = logging.getLogger("risklens.cache")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 3 days, in seconds. This is the ONLY place TTL is defined.
CACHE_TTL_SECONDS = 3 * 24 * 60 * 60  # 259200

# Bump this if the *shape* of cached payloads ever changes. Old entries
# written under a different version are naturally ignored because they'd
# live under a different key prefix — this avoids ever handing a tool a
# cached blob in a schema it doesn't expect.
CACHE_SCHEMA_VERSION = "v1"

CACHE_KEY_PREFIX = "risklens"

# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_redis_client: Optional[Redis] = None
_client_init_failed = False


def _get_client() -> Optional[Redis]:
    """
    Lazily create and return a singleton async Upstash Redis client.

    Returns None if credentials are missing or client construction fails,
    in which case all cache operations become safe no-ops. We only try
    constructing the client once per process (cached failure) so a missing
    .env doesn't retry-and-log on every single tool call.
    """
    global _redis_client, _client_init_failed

    if _redis_client is not None:
        return _redis_client

    if _client_init_failed:
        return None

    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")

    if not url or not token:
        logger.warning(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set — "
            "caching is disabled, tools will run uncached."
        )
        _client_init_failed = True
        return None

    try:
        _redis_client = Redis(url=url, token=token)
        logger.info("Upstash Redis client initialized.")
        return _redis_client
    except Exception:
        logger.exception("Failed to initialize Upstash Redis client.")
        _client_init_failed = True
        return None


# ---------------------------------------------------------------------------
# Key building
# ---------------------------------------------------------------------------


def build_cache_key(tool_name: str, **params: Any) -> str:
    """
    Build a deterministic, collision-resistant cache key for a tool call.

    Rules that make this consistent no matter who calls it or in what order
    kwargs are passed:
      - tool_name is always included verbatim (namespacing per tool).
      - params are sorted by key name before hashing, so build_cache_key(
        "x", a=1, b=2) and build_cache_key("x", b=2, a=1) produce the SAME
        key.
      - All param values are coerced to strings via json.dumps with
        sort_keys=True, so nested lists/dicts hash consistently too.
      - Ticker-like string params are uppercased and stripped so "aapl",
        " AAPL ", and "AAPL" all hit the same cache entry.

    Final key shape:
        risklens:v1:<tool_name>:<16-char-hash>

    The human-readable tool_name prefix makes keys greppable/debuggable in
    the Upstash console; the hash suffix guarantees uniqueness per param set.
    """
    normalized: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, str):
            v = v.strip()
            # Heuristic: short all-caps-able fields like tickers should be
            # case-insensitive in the cache key.
            if k in ("ticker", "symbol"):
                v = v.upper()
        normalized[k] = v

    canonical = json.dumps(normalized, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return f"{CACHE_KEY_PREFIX}:{CACHE_SCHEMA_VERSION}:{tool_name}:{digest}"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


async def get_cached(key: str) -> Optional[Any]:
    """
    Fetch and decode a cached value.

    Returns:
        - The original Python object (dict/list/etc.) that was cached, if
          a valid, parseable entry exists.
        - None if there's no entry, the entry is malformed, or Redis is
          unreachable. Callers should treat None as "cache miss" and just
          proceed to compute the result — this function NEVER raises.
    """
    client = _get_client()
    if client is None:
        return None

    try:
        raw = await client.get(key)
    except Exception:
        logger.exception("Redis GET failed for key=%s — treating as cache miss.", key)
        return None

    if raw is None:
        return None

    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Cache entry for key=%s was not valid JSON — ignoring.", key)
        return None

    if not isinstance(envelope, dict) or "data" not in envelope:
        logger.warning("Cache entry for key=%s missing expected envelope — ignoring.", key)
        return None

    return envelope["data"]


async def set_cached(key: str, data: Any, ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
    """
    Store a value in the cache with the given TTL (defaults to 3 days).

    Returns True if the write succeeded, False otherwise. Callers should
    NOT treat a False return as fatal — the tool result should still be
    returned to the user even if caching the result failed.
    """
    client = _get_client()
    if client is None:
        return False

    envelope = {
        "cached_at": int(time.time()),
        "ttl_seconds": ttl_seconds,
        "schema_version": CACHE_SCHEMA_VERSION,
        "data": data,
    }

    try:
        payload = json.dumps(envelope, default=str)
    except (TypeError, ValueError):
        logger.exception("Failed to JSON-serialize cache payload for key=%s — skipping cache write.", key)
        return False

    try:
        await client.set(key, payload, ex=ttl_seconds)
        return True
    except Exception:
        logger.exception("Redis SET failed for key=%s — result was NOT cached, but request will still complete.", key)
        return False


async def delete_cached(key: str) -> bool:
    """Delete a cache entry. Returns True if the delete call succeeded (even if the key didn't exist)."""
    client = _get_client()
    if client is None:
        return False

    try:
        await client.delete(key)
        return True
    except Exception:
        logger.exception("Redis DELETE failed for key=%s", key)
        return False


async def cache_health_check() -> dict[str, Any]:
    """
    Lightweight Redis connectivity check, used by the server's health
    endpoint / startup log so connection issues are visible immediately
    rather than discovered on a user's first tool call.
    """
    client = _get_client()
    if client is None:
        return {"connected": False, "reason": "client not initialized (missing credentials or init error)"}

    probe_key = f"{CACHE_KEY_PREFIX}:health:probe"
    try:
        await client.set(probe_key, "ok", ex=30)
        value = await client.get(probe_key)
        if value == "ok":
            return {"connected": True}
        return {"connected": False, "reason": f"unexpected probe value: {value!r}"}
    except Exception as e:
        return {"connected": False, "reason": str(e)}