"""
tools/eight_k_events.py
=========================
Implements the `analyze_8k_events` MCP tool.

Pulls a company's recent Form 8-K filings from SEC EDGAR and analyzes the
disclosed item codes for risk signal: restatements, bankruptcy/receivership,
delisting notices, accelerated debt obligations, impairments, leadership
upheaval, and so on.

Every filing in the response carries real, extracted DETAIL — actual text
pulled from the filing's own document, not just an item-code label — so a
reader (human or LLM) sees what was actually disclosed first. The risk
read (score, tier, narrative) is layered on top of that real content, not
a substitute for it. Flagged/high-risk filings additionally get the
complete per-item section text, since that's where a deeper read is worth
the extra document fetch; routine filings get a short excerpt and stay fast.

This tool also looks for CLUSTERING patterns across filings (e.g. multiple
director departures in a short window) that are easy to miss when scoring
each filing in isolation — turning "two 5.02 filings, neither individually
severe" into an explicit, explainable risk reason instead of staying
invisible inside a flat score.

Flexible by design: the same tool can return a neutral/plain summary of
recent 8-K activity (mode="summary") for people who just want "what has
this company filed lately" — still with real per-filing detail, just
without risk scoring/flagging language — while the default mode="risk"
leads with the risk read.

Caching: keyed on (ticker, lookback_days, mode, include_excerpts). Cache is
checked first; on a hit we return immediately without touching SEC EDGAR
or re-fetching/re-parsing any filing documents.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from core.cache import build_cache_key, get_cached, set_cached
from core.filing_content import build_excerpt, fetch_item_sections
from core.risk_rules import (
    CONTENT_SCORED_ITEMS,
    EIGHT_K_ITEMS,
    HIGH_RISK_8K_ITEMS,
    SEVERE_RISK_8K_ITEMS,
    classify_8k_item,
    detect_clusters,
    risk_tier_with_reason,
    score_5_02_from_text,
    score_to_risk_level,
)
from core.sec_client import fetch_submissions, resolve_ticker_to_cik

logger = logging.getLogger("risklens.tools.eight_k")

TOOL_NAME = "8k_events"

# Full per-item section text is only fetched for the highest-priority
# flagged filings (weight >= 2 or part of a detected cluster) — this keeps
# routine calls fast, per the deliberate latency/depth tradeoff for this tool.
MAX_FILINGS_FOR_FULL_TEXT = 5

# Every filing in the response gets a short `details` excerpt regardless of
# risk level, so the reader has real content to look at — not just an item
# code — even for routine filings. Capped separately from full-text fetches
# so a company with many filings in the lookback window doesn't trigger an
# unbounded number of document fetches on a single call.
MAX_FILINGS_FOR_DETAILS = 15


async def analyze_8k_events(
    ticker: str,
    lookback_days: int = 180,
    mode: Literal["risk", "summary"] = "risk",
    include_excerpts: bool = True,
) -> dict[str, Any]:
    """
    Analyze a company's recent 8-K filings on SEC EDGAR.

    Args:
        ticker: Stock ticker symbol, e.g. "AAPL", "TSLA". Case-insensitive.
        lookback_days: How many days back to look for 8-K filings. Default 180.
            Clamped to the range [1, 1825] (5 years) to keep responses sane.
        mode: "risk" (default) returns a risk-focused analysis: severity
            scoring, clustering detection, real per-filing detail excerpts,
            full text on the highest-priority flagged filings, and a
            plain-language risk narrative with a concrete reason. "summary"
            returns a neutral, non-judgmental list of recent 8-K filings —
            still with real per-filing detail excerpts — and no risk framing,
            useful when someone just wants to know what a company has filed
            lately.
        include_excerpts: When True (default), fetches each filing's actual
            document and returns a real, extracted "details" excerpt per
            disclosed item (not just the item code) on every filing, plus
            full per-item section text on the highest-priority flagged
            filings in risk mode. Set False to skip document fetches
            entirely and get a faster, metadata-only response (item codes
            and labels only, no real filing text).

    Returns:
        A dict with the analysis. Always includes "from_cache": bool so
        callers/clients can tell whether this was served from cache.
    """
    lookback_days = max(1, min(int(lookback_days), 1825))
    ticker_clean = ticker.strip().upper()

    cache_key = build_cache_key(
        TOOL_NAME, ticker=ticker_clean, lookback_days=lookback_days, mode=mode, include_excerpts=include_excerpts
    )

    cached = await get_cached(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    result = await _run_analysis(ticker_clean, lookback_days, mode, include_excerpts)

    if not result.get("error"):
        await set_cached(cache_key, result)

    return {**result, "from_cache": False}


async def _run_analysis(ticker: str, lookback_days: int, mode: str, include_excerpts: bool) -> dict[str, Any]:
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
        return await _build_summary_response(ticker, company, cik, lookback_days, filings, include_excerpts)
    return await _build_risk_response(ticker, company, cik, lookback_days, filings, include_excerpts)


def _extract_8k_filings(submissions: dict[str, Any], cik: str, lookback_days: int) -> list[dict[str, Any]]:
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


async def _build_summary_response(
    ticker: str,
    company: dict[str, str],
    cik: str,
    lookback_days: int,
    filings: list[dict[str, Any]],
    include_excerpts: bool,
) -> dict[str, Any]:
    if include_excerpts:
        detail_targets = filings[:MAX_FILINGS_FOR_DETAILS]
        section_results = await asyncio.gather(
            *[
                fetch_item_sections(cik, f["accession_number"], f.get("primary_document"))
                for f in detail_targets
            ],
            return_exceptions=True,
        )
        content_by_accession: dict[str, dict[str, str]] = {}
        for f, sections in zip(detail_targets, section_results):
            if isinstance(sections, Exception):
                logger.warning("Content fetch failed for accession=%s: %s", f.get("accession_number"), sections)
                sections = {}
            content_by_accession[f["accession_number"]] = sections
    else:
        content_by_accession = {}

    plain_filings = []
    for f in filings:
        item_labels = [d["label"] for d in f["items_detail"]]
        sections = content_by_accession.get(f["accession_number"], {})
        plain_filings.append(
            {
                "filing_date": f["filing_date"],
                "form_type": f["form_type"],
                "items": f["item_codes"],
                "item_labels": item_labels,
                "filing_url": f["filing_url"],
                "details": {code: build_excerpt(text) for code, text in sections.items()},
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


async def _build_risk_response(
    ticker: str,
    company: dict[str, str],
    cik: str,
    lookback_days: int,
    filings: list[dict[str, Any]],
    include_excerpts: bool,
) -> dict[str, Any]:
    company_name = company.get("name") or ticker

    # --- Step 1: Fetch content for ALL filings first (bounded) so we can
    # use real filing text to upgrade item weights BEFORE scoring.
    # This is the key fix: content-based severity detection needs the text
    # before we decide what's flagged, not after.
    if include_excerpts:
        detail_targets = filings[:MAX_FILINGS_FOR_DETAILS]
        section_results = await asyncio.gather(
            *[
                fetch_item_sections(cik, f["accession_number"], f.get("primary_document"))
                for f in detail_targets
            ],
            return_exceptions=True,
        )
        filing_content_by_accession: dict[str, dict[str, str]] = {}
        for f, sections in zip(detail_targets, section_results):
            if isinstance(sections, Exception):
                logger.warning("Content fetch failed for accession=%s: %s", f.get("accession_number"), sections)
                sections = {}
            filing_content_by_accession[f["accession_number"]] = sections
    else:
        filing_content_by_accession = {}

    # --- Step 2: Build item hits, applying content-based weight upgrades
    # for items in CONTENT_SCORED_ITEMS (currently 5.02). Every 5.02 item
    # now gets its text scanned for role keywords (CEO, CFO, COO, President,
    # Chairman, etc.) so a CEO transition scores very differently from a
    # routine accounting-officer change — even though both are "Item 5.02".
    all_item_hits: list[dict[str, Any]] = []
    content_signals: dict[str, dict[str, Any]] = {}  # accession -> content signal

    for f in filings:
        sections = filing_content_by_accession.get(f["accession_number"], {})
        for detail in f["items_detail"]:
            item_entry = {**detail, "filing_date": f["filing_date"], "filing_url": f["filing_url"]}

            if detail["code"] in CONTENT_SCORED_ITEMS and sections.get(detail["code"]):
                signal = score_5_02_from_text(sections[detail["code"]])
                if signal["escalated_weight"] > int(detail["weight"]):
                    item_entry = {
                        **item_entry,
                        "weight": signal["escalated_weight"],
                        "content_risk_label": signal["content_risk_label"],
                        "detected_title": signal["detected_title"],
                    }
                    content_signals[f["accession_number"]] = signal

            all_item_hits.append(item_entry)

    # --- Step 3: Compute risk score from (possibly upgraded) weights
    high_risk_hits = [h for h in all_item_hits if int(h["weight"]) >= 2]
    severe_hits = [h for h in all_item_hits if int(h["weight"]) >= 3]

    weight_to_floor = {3: 7.5, 2: 5.0, 1: 2.0, 0: 0.0}
    max_weight = max((int(h["weight"]) for h in all_item_hits), default=0)
    severity_floor = weight_to_floor.get(max_weight, 0.0)
    extra_weight = sum(float(h["weight"]) for h in all_item_hits) - max_weight
    breadth_bonus = min(2.5, extra_weight * 0.5)
    base_score = severity_floor + breadth_bonus if all_item_hits else 0.0

    clusters = detect_clusters(filings)
    cluster_bump = sum(c["score_bump"] for c in clusters)

    normalized_score = min(10.0, base_score + cluster_bump)
    risk_level = score_to_risk_level(normalized_score)

    # Build risk reason — prefer content-detected signals over generic ones
    # so "CEO transition — Chief Executive Officer named in filing" is the
    # reason rather than a bare "elevated-risk disclosure category present".
    content_reason = None
    if content_signals:
        # pick the highest-weight content signal to lead the reason
        best_signal = max(content_signals.values(), key=lambda s: s["escalated_weight"])
        if best_signal["escalated_weight"] >= 2:
            content_reason = best_signal["content_risk_label"]

    severe_labels = sorted({h.get("content_risk_label") or h["label"] for h in severe_hits})
    cluster_reasons = [c["reason"] for c in clusters]
    tier = risk_tier_with_reason(
        normalized_score,
        cluster_reasons,
        severe_labels if severe_labels else ([content_reason] if content_reason else []),
    )

    category_counts: dict[str, int] = {}
    for h in all_item_hits:
        category_counts[h["category"]] = category_counts.get(h["category"], 0) + 1

    # Flagged filings: weight >= 2 (now includes content-upgraded items),
    # OR part of a detected cluster, OR has a content signal (CEO/CFO/etc.)
    flagged_filings = [
        f
        for f in filings
        if any(int(d["weight"]) >= 2 for d in f["items_detail"])
        or any(f["filing_date"] in c.get("matched_dates", []) for c in clusters)
        or f["accession_number"] in content_signals
    ]

    # --- Step 4: Full text for flagged filings only
    full_text_accessions = {f["accession_number"] for f in flagged_filings[:MAX_FILINGS_FOR_FULL_TEXT]}

    def _filing_details(f: dict[str, Any]) -> dict[str, Any]:
        sections = filing_content_by_accession.get(f["accession_number"], {})
        item_excerpts = {code: build_excerpt(text) for code, text in sections.items()}
        full_text = (
            {code: text for code, text in sections.items()}
            if f["accession_number"] in full_text_accessions
            else {}
        )
        # Attach content signal if present so the output explicitly says what
        # role was detected (e.g. "detected_title: Chief Executive Officer")
        signal = content_signals.get(f["accession_number"])
        return {
            "item_excerpts": item_excerpts,
            "item_full_text": full_text,
            "content_available": bool(sections),
            "content_signal": {
                "detected_title": signal["detected_title"],
                "risk_label": signal["content_risk_label"],
            } if signal else None,
        }

    excerpted_filings: list[dict[str, Any]] = []
    if include_excerpts:
        for f in flagged_filings[:MAX_FILINGS_FOR_FULL_TEXT]:
            details = _filing_details(f)
            excerpted_filings.append(
                {
                    "filing_date": f["filing_date"],
                    "items": f["item_codes"],
                    "item_labels": [d["label"] for d in f["items_detail"]],
                    "filing_url": f["filing_url"],
                    "item_full_text": details["item_full_text"],
                    "content_signal": details["content_signal"],
                    "content_available": details["content_available"],
                }
            )

    narrative = _risk_narrative(
        company_name, lookback_days, filings, severe_hits, high_risk_hits, clusters, tier,
        content_signals=content_signals,
    )


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
        "risk_tier_label": tier["tier_label"],
        "risk_reason": tier["reason"],
        "severe_event_count": len(severe_hits),
        "high_risk_event_count": len(high_risk_hits),
        "risk_category_breakdown": category_counts,
        "clusters_detected": clusters,
        "flagged_filings": [
            {
                "filing_date": f["filing_date"],
                "items": f["item_codes"],
                "item_labels": [d["label"] for d in f["items_detail"]],
                "filing_url": f["filing_url"],
                "content_signal": content_signals.get(f["accession_number"]) and {
                    "detected_title": content_signals[f["accession_number"]]["detected_title"],
                    "risk_label": content_signals[f["accession_number"]]["content_risk_label"],
                },
            }
            for f in flagged_filings
        ],
        "flagged_filings_full_text": excerpted_filings,
        "all_filings": [
            {
                "filing_date": f["filing_date"],
                "items": f["item_codes"],
                "item_labels": [d["label"] for d in f["items_detail"]],
                "filing_url": f["filing_url"],
                "details": _filing_details(f)["item_excerpts"],
            }
            for f in filings
        ],
        "narrative": narrative,
        "disclaimer": (
            "This analysis is derived from SEC EDGAR item codes, clustering patterns across "
            "filings, and real extracted text from the underlying filing documents. It is not "
            "investment advice. The 'details' and 'item_full_text' fields are verbatim excerpts "
            "from the filer's own disclosure (not commentary or interpretation) — read them in "
            "context via the filing_url before drawing conclusions."
        ),
    }


def _risk_narrative(
    company_name: str,
    lookback_days: int,
    filings: list[dict[str, Any]],
    severe_hits: list[dict[str, Any]],
    high_risk_hits: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    tier: dict[str, str],
    content_signals: Optional[dict[str, Any]] = None,
) -> str:
    if not filings:
        return (
            f"{company_name} filed no Form 8-K reports in the last {lookback_days} days. "
            "No 8-K-driven risk signal to assess for this window."
        )

    parts = [f"{company_name}'s 8-K filings over the last {lookback_days} days: {tier['tier_label']}."]

    # Content signals take priority in the narrative — these are real,
    # specific events detected in the actual filing text (e.g. CEO departure),
    # not just generic item-code labels. Naming what happened specifically
    # is far more useful than "elevated-risk disclosure category present".
    if content_signals:
        best = max(content_signals.values(), key=lambda s: s["escalated_weight"])
        if best["detected_title"]:
            parts.append(
                f"Content analysis of the actual filing text identified a significant leadership "
                f"event: {best['content_risk_label']}. This type of disclosure is conventionally "
                f"considered material by investors regardless of whether it appears with other "
                f"risk signals in the same window."
            )

    if severe_hits:
        # Only add a separate "severe disclosures" sentence if it adds
        # information beyond what the content signal already said.
        non_content_severe = [h for h in severe_hits if not h.get("content_risk_label")]
        if non_content_severe:
            labels = sorted({h["label"] for h in non_content_severe})
            parts.append(
                f"{len(non_content_severe)} severe-category disclosure(s) found, including: "
                f"{'; '.join(labels[:4])}. These categories are the ones SEC filers use for events "
                "like restatements, bankruptcy, delisting, or accelerated debt obligations — worth "
                "direct follow-up via the filing text itself."
            )
        elif not content_signals:
            labels = sorted({h.get("content_risk_label") or h["label"] for h in severe_hits})
            parts.append(
                f"{len(severe_hits)} severe-category disclosure(s) found, including: "
                f"{'; '.join(labels[:4])}. These categories are the ones SEC filers use for events "
                "like restatements, bankruptcy, delisting, or accelerated debt obligations — worth "
                "direct follow-up via the filing text itself."
            )
    elif high_risk_hits and not content_signals:
        labels = sorted({h.get("content_risk_label") or h["label"] for h in high_risk_hits})
        parts.append(
            f"{len(high_risk_hits)} elevated-risk disclosure(s): {'; '.join(labels[:4])}. "
            "No severe-category items (e.g. bankruptcy, restatement, delisting) were found "
            "in this window."
        )

    if clusters:
        for c in clusters[:3]:
            parts.append(f"Pattern detected: {c['reason']}.")
    elif not severe_hits and not high_risk_hits and not content_signals:
        parts.append(
            f"All {len(filings)} filing(s) fall into routine or low-risk categories "
            "(e.g. earnings releases, exhibits, governance housekeeping), and no clustering "
            "patterns were detected across the filing history in this window."
        )

    return " ".join(parts)