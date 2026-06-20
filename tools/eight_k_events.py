"""
tools/eight_k_events.py
=========================
Implements the `analyze_8k_events` MCP tool.

Pulls a company's recent Form 8-K filings from SEC EDGAR and analyzes the
disclosed item codes for risk signal: restatements, bankruptcy/receivership,
delisting notices, accelerated debt obligations, impairments, leadership
upheaval, and so on.

Flexible by design: the same tool can return a neutral/plain summary of
recent 8-K activity (mode="summary") for people who just want "what has
this company filed lately", while the default mode="risk" leads with the
risk read.

Caching: keyed on (ticker, lookback_days, mode). Cache is checked first;
on a hit we return immediately without touching SEC EDGAR at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from core.cache import build_cache_key, get_cached, set_cached
from core.risk_rules import (
    EIGHT_K_ITEMS,
    HIGH_RISK_8K_ITEMS,
    SEVERE_RISK_8K_ITEMS,
    classify_8k_item,
    score_to_risk_level,
)
from core.sec_client import fetch_submissions, resolve_ticker_to_cik

logger = logging.getLogger("risklens.tools.eight_k")

TOOL_NAME = "8k_events"


async def analyze_8k_events(
    ticker: str,
    lookback_days: int = 180,
    mode: Literal["risk", "summary"] = "risk",
) -> dict[str, Any]:
    """
    Analyze a company's recent 8-K filings on SEC EDGAR.

    Args:
        ticker: Stock ticker symbol, e.g. "AAPL", "TSLA". Case-insensitive.
        lookback_days: How many days back to look for 8-K filings. Default 180.
            Clamped to the range [1, 1825] (5 years) to keep responses sane.
        mode: "risk" (default) returns a risk-focused analysis: severity
            scoring, flagged items, and a plain-language risk narrative.
            "summary" returns a neutral, non-judgmental list of recent
            8-K filings and what each one disclosed, with no risk framing —
            useful when someone just wants to know what a company has
            filed lately.

    Returns:
        A dict with the analysis. Always includes "from_cache": bool so
        callers/clients can tell whether this was served from cache.
    """
    lookback_days = max(1, min(int(lookback_days), 1825))
    ticker_clean = ticker.strip().upper()

    cache_key = build_cache_key(TOOL_NAME, ticker=ticker_clean, lookback_days=lookback_days, mode=mode)

    cached = await get_cached(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    result = await _run_analysis(ticker_clean, lookback_days, mode)

    # Only cache successful analyses — never cache error responses, so a
    # transient SEC EDGAR failure doesn't get "stuck" in the cache for
    # 3 days.
    if not result.get("error"):
        await set_cached(cache_key, result)

    return {**result, "from_cache": False}


async def _run_analysis(ticker: str, lookback_days: int, mode: str) -> dict[str, Any]:
    company = await resolve_ticker_to_cik(ticker)
    if company is None:
        return {
            "error": True,
            "error_message": (
                f"Could not resolve ticker '{ticker}' to a company on SEC EDGAR. "
                "Double-check the ticker symbol — only US-listed companies that "
                "file with the SEC are covered."
            ),
            "ticker": ticker,
        }

    cik = company["cik"]
    submissions = await fetch_submissions(cik)
    if submissions is None:
        return {
            "error": True,
            "error_message": f"SEC EDGAR returned no submissions data for CIK {cik} ({ticker}).",
            "ticker": ticker,
            "company_name": company.get("name"),
            "cik": cik,
        }

    filings = _extract_8k_filings(submissions, cik, lookback_days)

    if mode == "summary":
        return _build_summary_response(ticker, company, cik, lookback_days, filings)
    return _build_risk_response(ticker, company, cik, lookback_days, filings)


def _extract_8k_filings(submissions: dict[str, Any], cik: str, lookback_days: int) -> list[dict[str, Any]]:
    """
    Pull 8-K (and 8-K/A) filings out of the EDGAR submissions JSON, within
    the lookback window, returning them newest-first with parsed item codes.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    items_raw = recent.get("items", [""] * len(forms))
    primary_docs = recent.get("primaryDocument", [""] * len(forms))
    descriptions = recent.get("primaryDocDescription", [""] * len(forms))

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)

    filings: list[dict[str, Any]] = []
    for i, form in enumerate(forms):
        if not form or not form.upper().startswith("8-K"):
            continue

        try:
            filing_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue

        if filing_date < cutoff:
            continue

        raw_items = items_raw[i] if i < len(items_raw) else ""
        item_codes = [code.strip() for code in raw_items.split(",") if code.strip()] if raw_items else []

        filings.append(
            {
                "form_type": form,
                "filing_date": dates[i],
                "accession_number": accession_numbers[i] if i < len(accession_numbers) else None,
                "item_codes": item_codes,
                "items_detail": [classify_8k_item(c) for c in item_codes],
                "primary_document": primary_docs[i] if i < len(primary_docs) else None,
                "description": descriptions[i] if i < len(descriptions) else None,
                "filing_url": _filing_url(cik, accession_numbers[i] if i < len(accession_numbers) else None),
            }
        )

    filings.sort(key=lambda f: f["filing_date"], reverse=True)
    return filings


def _filing_url(cik: str, accession_number: Optional[str]) -> Optional[str]:
    if not accession_number:
        return None
    acc_nodash = accession_number.replace("-", "")
    cik_int = str(int(cik))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession_number}-index.htm"


def _build_summary_response(
    ticker: str, company: dict[str, str], cik: str, lookback_days: int, filings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Neutral, non-judgmental view: what was filed, plainly described."""
    plain_filings = []
    for f in filings:
        item_labels = [d["label"] for d in f["items_detail"]]
        plain_filings.append(
            {
                "filing_date": f["filing_date"],
                "form_type": f["form_type"],
                "items": f["item_codes"],
                "item_labels": item_labels,
                "filing_url": f["filing_url"],
            }
        )

    return {
        "error": False,
        "mode": "summary",
        "ticker": ticker,
        "company_name": company.get("name"),
        "cik": cik,
        "lookback_days": lookback_days,
        "filing_count": len(filings),
        "filings": plain_filings,
        "narrative": (
            f"{company.get('name') or ticker} filed {len(filings)} Form 8-K "
            f"report(s) in the last {lookback_days} days."
            if filings
            else f"No Form 8-K filings found for {company.get('name') or ticker} "
            f"in the last {lookback_days} days."
        ),
    }


def _build_risk_response(
    ticker: str, company: dict[str, str], cik: str, lookback_days: int, filings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Risk-focused view: severity scoring, flagged events, narrative."""
    all_item_hits: list[dict[str, Any]] = []
    for f in filings:
        for detail in f["items_detail"]:
            all_item_hits.append({**detail, "filing_date": f["filing_date"], "filing_url": f["filing_url"]})

    high_risk_hits = [h for h in all_item_hits if h["code"] in HIGH_RISK_8K_ITEMS]
    severe_hits = [h for h in all_item_hits if h["code"] in SEVERE_RISK_8K_ITEMS]

    # Score model: anchored on the single worst item found (so one severe
    # disclosure, e.g. a restatement, is never diluted into a moderate
    # score just because most of a company's other filings are routine),
    # plus a smaller "breadth" term so repeated/clustered risk items still
    # push the score higher than a single isolated one.
    #
    # weight 3 (severe) item alone   -> floor of 7.5 (SEVERE band)
    # weight 2 (elevated) item alone -> floor of 5.0 (ELEVATED band)
    # weight 1 (mild) item alone     -> floor of 2.0 (MODERATE-ish, not LOW)
    # weight 0 items contribute nothing to the floor.
    weight_to_floor = {3: 7.5, 2: 5.0, 1: 2.0, 0: 0.0}
    max_weight = max((int(h["weight"]) for h in all_item_hits), default=0)
    severity_floor = weight_to_floor.get(max_weight, 0.0)

    # Extra contribution from additional risk-bearing items beyond the
    # single worst one (so repeated/clustered severe events score higher
    # than one isolated severe event, without diluting a single event
    # below its proper severity floor).
    extra_weight = sum(float(h["weight"]) for h in all_item_hits) - max_weight
    breadth_bonus = min(2.5, extra_weight * 0.5)

    normalized_score = min(10.0, severity_floor + breadth_bonus) if all_item_hits else 0.0
    risk_level = score_to_risk_level(normalized_score)

    category_counts: dict[str, int] = {}
    for h in all_item_hits:
        category_counts[h["category"]] = category_counts.get(h["category"], 0) + 1

    flagged_filings = [
        {
            "filing_date": f["filing_date"],
            "items": [d["code"] for d in f["items_detail"] if d["weight"] >= 2],
            "item_labels": [d["label"] for d in f["items_detail"] if d["weight"] >= 2],
            "filing_url": f["filing_url"],
        }
        for f in filings
        if any(d["weight"] >= 2 for d in f["items_detail"])
    ]

    narrative = _risk_narrative(company.get("name") or ticker, lookback_days, filings, severe_hits, high_risk_hits, risk_level)

    return {
        "error": False,
        "mode": "risk",
        "ticker": ticker,
        "company_name": company.get("name"),
        "cik": cik,
        "lookback_days": lookback_days,
        "filing_count": len(filings),
        "risk_score": round(normalized_score, 1),
        "risk_level": risk_level,
        "severe_event_count": len(severe_hits),
        "high_risk_event_count": len(high_risk_hits),
        "risk_category_breakdown": category_counts,
        "flagged_filings": flagged_filings,
        "all_filings": [
            {
                "filing_date": f["filing_date"],
                "items": f["item_codes"],
                "item_labels": [d["label"] for d in f["items_detail"]],
                "filing_url": f["filing_url"],
            }
            for f in filings
        ],
        "narrative": narrative,
        "disclaimer": (
            "This analysis is derived solely from SEC EDGAR item codes and is "
            "not investment advice. It highlights disclosure patterns conventionally "
            "associated with elevated risk; it does not assess whether the underlying "
            "events were ultimately material, resolved, or financially significant."
        ),
    }


def _risk_narrative(
    company_name: str,
    lookback_days: int,
    filings: list[dict[str, Any]],
    severe_hits: list[dict[str, Any]],
    high_risk_hits: list[dict[str, Any]],
    risk_level: str,
) -> str:
    if not filings:
        return (
            f"{company_name} filed no Form 8-K reports in the last {lookback_days} days. "
            "No 8-K-driven risk signal to assess for this window."
        )

    if severe_hits:
        labels = sorted({h["label"] for h in severe_hits})
        return (
            f"{company_name}'s 8-K filings over the last {lookback_days} days show "
            f"{risk_level} risk signal. {len(severe_hits)} severe-category disclosure(s) "
            f"were found, including: {'; '.join(labels[:4])}. These are the categories "
            "SEC filers most often use for events like restatements, bankruptcy, "
            "delisting, or accelerated debt obligations — worth direct follow-up."
        )

    if high_risk_hits:
        labels = sorted({h["label"] for h in high_risk_hits})
        return (
            f"{company_name}'s 8-K filings over the last {lookback_days} days show "
            f"{risk_level} risk signal, driven by {len(high_risk_hits)} elevated-risk "
            f"disclosure(s): {'; '.join(labels[:4])}. No severe-category items (e.g. "
            "bankruptcy, restatement, delisting) were found in this window."
        )

    return (
        f"{company_name} filed {len(filings)} Form 8-K report(s) in the last "
        f"{lookback_days} days, all in routine or low-risk categories (e.g. earnings "
        f"releases, exhibits, governance housekeeping). Risk level: {risk_level}."
    )