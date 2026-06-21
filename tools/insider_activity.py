"""
tools/insider_activity.py
============================
Implements the `analyze_insider_activity` MCP tool.

Pulls a company's recent Form 4 (insider transaction) filings from SEC
EDGAR, parses the actual transaction XML for each filing, and analyzes the
pattern for risk/conviction signal: clusters of open-market selling,
distressed-looking sales by multiple officers in a short window, vs. routine
compensation-driven activity (option exercises, tax withholding, grants).

Flexible by design: mode="risk" (default) leads with the risk read.
mode="summary" returns a neutral list of who-bought/who-sold-what, with no
risk framing, for people who just want the raw activity.

Caching: keyed on (ticker, lookback_days, mode, max_filings).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from xml.etree import ElementTree as ET

from core.cache import build_cache_key, get_cached, set_cached
from core.risk_rules import OPEN_MARKET_CODES, classify_form4_code, score_to_risk_level
from core.sec_client import fetch_document_text, fetch_filing_index_json, fetch_submissions, resolve_ticker_to_cik

logger = logging.getLogger("risklens.tools.insider_activity")

TOOL_NAME = "insider_activity"

# Cap how many individual Form 4 filings we'll fetch+parse per call, to
# keep latency and SEC EDGAR load bounded on a single tool invocation.
MAX_FILINGS_HARD_CAP = 40


async def analyze_insider_activity(
    ticker: str,
    lookback_days: int = 90,
    mode: Literal["risk", "summary"] = "risk",
    max_filings: int = 25,
) -> dict[str, Any]:
    """
    Analyze a company's recent Form 4 insider transaction filings on SEC EDGAR.

    Args:
        ticker: Stock ticker symbol, e.g. "AAPL", "TSLA". Case-insensitive.
        lookback_days: How many days back to look for Form 4 filings. Default 90.
            Clamped to [1, 730] (2 years).
        mode: "risk" (default) returns a risk-focused read: clustering of
            open-market sells/buys, officer/director concentration, and a
            plain-language conviction narrative. "summary" returns a neutral
            transaction-by-transaction list with no risk framing.
        max_filings: Max number of individual Form 4 filings to fetch and
            parse for this call. Default 25, hard-capped at 40 to keep
            response times reasonable.

    Returns:
        A dict with the analysis. Always includes "from_cache": bool.
    """
    lookback_days = max(1, min(int(lookback_days), 730))
    max_filings = max(1, min(int(max_filings), MAX_FILINGS_HARD_CAP))
    ticker_clean = ticker.strip().upper()

    cache_key = build_cache_key(
        TOOL_NAME, ticker=ticker_clean, lookback_days=lookback_days, mode=mode, max_filings=max_filings
    )

    cached = await get_cached(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    result = await _run_analysis(ticker_clean, lookback_days, mode, max_filings)

    if not result.get("error"):
        await set_cached(cache_key, result)

    return {**result, "from_cache": False}


async def _run_analysis(ticker: str, lookback_days: int, mode: str, max_filings: int) -> dict[str, Any]:
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

    form4_filings_meta = _extract_form4_filing_refs(submissions, lookback_days, max_filings)

    logger.info(
        "DIAG: cik=%s ticker=%s lookback_days=%d -> found %d Form 4 filing ref(s) in window",
        cik, ticker, lookback_days, len(form4_filings_meta),
    )

    if not form4_filings_meta:
        empty = _empty_result(ticker, company, cik, lookback_days, mode)
        return empty

    # Fetch + parse all candidate Form 4 filings concurrently (bounded by
    # the shared SEC rate limiter inside sec_client, so this is safe even
    # though we launch them together).
    transactions = await _fetch_and_parse_all(cik, form4_filings_meta)

    if mode == "summary":
        return _build_summary_response(ticker, company, cik, lookback_days, transactions)
    return _build_risk_response(ticker, company, cik, lookback_days, transactions)


def _extract_form4_filing_refs(
    submissions: dict[str, Any], lookback_days: int, max_filings: int
) -> list[dict[str, Any]]:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)

    refs = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        try:
            filing_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff:
            continue
        refs.append(
            {
                "filing_date": dates[i],
                "accession_number": accession_numbers[i] if i < len(accession_numbers) else None,
                "primary_document": primary_docs[i] if i < len(primary_docs) else None,
            }
        )

    refs.sort(key=lambda r: r["filing_date"], reverse=True)
    return refs[:max_filings]


async def _fetch_and_parse_all(cik: str, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(5)

    async def _one(ref: dict[str, Any]) -> list[dict[str, Any]]:
        async with sem:
            return await _fetch_and_parse_form4(cik, ref)

    results = await asyncio.gather(*[_one(r) for r in refs], return_exceptions=True)

    transactions: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Form 4 fetch/parse failed: %s", r)
            continue
        transactions.extend(r)
    return transactions


async def _fetch_and_parse_form4(cik: str, ref: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Fetch the XML for one Form 4 filing and parse it into transaction records.
    Returns [] (not an exception) on any parse/fetch problem for THIS single
    filing — one bad filing shouldn't take down the whole analysis.
    """
    accession_number = ref.get("accession_number")
    if not accession_number:
        logger.warning("DIAG: ref has no accession_number: %r", ref)
        return []

    xml_filename = await _find_form4_xml_filename(cik, accession_number, ref.get("primary_document"))
    if not xml_filename:
        logger.warning(
            "DIAG: no xml_filename resolved for cik=%s accession=%s primary_document=%r",
            cik, accession_number, ref.get("primary_document"),
        )
        return []

    logger.info("DIAG: resolved xml_filename=%r for accession=%s", xml_filename, accession_number)

    xml_text = await fetch_document_text(cik, accession_number, xml_filename)
    if not xml_text:
        logger.warning(
            "DIAG: fetch_document_text returned empty for cik=%s accession=%s filename=%s",
            cik, accession_number, xml_filename,
        )
        return []

    logger.info("DIAG: fetched %d chars of XML/text for accession=%s", len(xml_text), accession_number)

    try:
        parsed = _parse_form4_xml(xml_text, filing_date=ref["filing_date"], accession_number=accession_number, cik=cik)
        logger.info("DIAG: parsed %d transaction(s) from accession=%s", len(parsed), accession_number)
        return parsed
    except ET.ParseError as e:
        logger.warning("Form 4 XML parse error for accession %s: %s", accession_number, e)
        logger.warning("DIAG: first 500 chars of unparseable content: %r", xml_text[:500])
        return []


async def _find_form4_xml_filename(cik: str, accession_number: str, primary_document: Optional[str]) -> Optional[str]:
    """
    Determine the actual ownership XML filename inside a Form 4 filing folder.
    Most filings' primaryDocument IS the XML (or an xslt-rendered view of it);
    fall back to the filing's index.json if needed.
    """
    if primary_document and primary_document.lower().endswith(".xml"):
        return primary_document

    index = await fetch_filing_index_json(cik, accession_number)
    if not index:
        return None

    items = index.get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "")
        if name.lower().endswith(".xml") and "ownership" in name.lower():
            return name
    for item in items:
        name = item.get("name", "")
        if name.lower().endswith(".xml"):
            return name
    return None


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------


def _local_tag(tag: str) -> str:
    """Strip XML namespace prefix from a tag name, e.g. '{ns}foo' -> 'foo'."""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_text(elem: Optional[ET.Element], path: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(path)
    if found is None:
        return None
    value_node = found.find("value")
    if value_node is not None and value_node.text is not None:
        return value_node.text.strip()
    if found.text is not None:
        return found.text.strip()
    return None


def _parse_form4_xml(xml_text: str, filing_date: str, accession_number: str, cik: str) -> list[dict[str, Any]]:
    """
    Parse an ownershipDocument XML into a flat list of transaction dicts.
    Handles both non-derivative and derivative transactions.
    """
    # Strip XML declaration weirdness / leading whitespace that sometimes
    # precedes the root element in EDGAR-served files.
    cleaned = xml_text.strip()
    match = re.search(r"<ownershipDocument[\s\S]*</ownershipDocument>", cleaned)
    if match:
        cleaned = match.group(0)

    root = ET.fromstring(cleaned)

    issuer = root.find("issuer")
    issuer_name = _find_raw_text(issuer, "issuerName")
    issuer_symbol = _find_raw_text(issuer, "issuerTradingSymbol")

    owners = root.findall("reportingOwner")
    owner_names = []
    owner_is_officer = False
    owner_is_director = False
    owner_titles = []
    for owner in owners:
        owner_id = owner.find("reportingOwnerId")
        name = _find_raw_text(owner_id, "rptOwnerName") if owner_id is not None else None
        if name:
            owner_names.append(name)
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            if _find_raw_text(rel, "isOfficer") == "1":
                owner_is_officer = True
            if _find_raw_text(rel, "isDirector") == "1":
                owner_is_director = True
            title = _find_raw_text(rel, "officerTitle")
            if title:
                owner_titles.append(title)

    owner_name_str = ", ".join(owner_names) if owner_names else "Unknown reporting owner"

    transactions: list[dict[str, Any]] = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        transactions.append(_parse_single_transaction(txn, "non_derivative"))
    for txn in root.findall(".//derivativeTransaction"):
        transactions.append(_parse_single_transaction(txn, "derivative"))

    enriched = []
    for t in transactions:
        if t is None:
            continue
        enriched.append(
            {
                **t,
                "issuer_name": issuer_name,
                "issuer_symbol": issuer_symbol,
                "owner_name": owner_name_str,
                "owner_is_officer": owner_is_officer,
                "owner_is_director": owner_is_director,
                "owner_titles": owner_titles,
                "filing_date": filing_date,
                "accession_number": accession_number,
                "filing_url": _form4_filing_url(cik, accession_number),
            }
        )
    return enriched


def _find_raw_text(elem: Optional[ET.Element], child_tag: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(child_tag)
    if found is not None and found.text:
        return found.text.strip()
    return None


def _parse_single_transaction(txn: ET.Element, kind: str) -> Optional[dict[str, Any]]:
    try:
        security_title = _find_text(txn, "securityTitle")
        transaction_date = _find_text(txn, "transactionDate")

        coding = txn.find("transactionCoding")
        code = _find_raw_text(coding, "transactionCode") if coding is not None else None

        amounts = txn.find("transactionAmounts")
        shares_str = _find_text(amounts, "transactionShares") if amounts is not None else None
        price_str = _find_text(amounts, "transactionPricePerShare") if amounts is not None else None
        acquired_disposed = _find_text(amounts, "transactionAcquiredDisposedCode") if amounts is not None else None

        post_amounts = txn.find("postTransactionAmounts")
        shares_owned_after_str = (
            _find_text(post_amounts, "sharesOwnedFollowingTransaction") if post_amounts is not None else None
        )

        shares = _safe_float(shares_str)
        price = _safe_float(price_str)
        shares_owned_after = _safe_float(shares_owned_after_str)

        if not code:
            return None

        code_meta = classify_form4_code(code)

        return {
            "kind": kind,
            "security_title": security_title,
            "transaction_date": transaction_date,
            "transaction_code": code,
            "transaction_code_label": code_meta["label"],
            "is_open_market": code_meta["is_open_market"],
            "is_compensation_driven": code_meta["is_compensation_driven"],
            "acquired_or_disposed": acquired_disposed,  # "A" or "D"
            "shares": shares,
            "price_per_share": price,
            "estimated_value": round(shares * price, 2) if shares is not None and price is not None else None,
            "shares_owned_after": shares_owned_after,
        }
    except Exception:
        logger.exception("Failed to parse a single Form 4 transaction element.")
        return None


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _form4_filing_url(cik: str, accession_number: str) -> str:
    acc_nodash = accession_number.replace("-", "")
    cik_int = str(int(cik))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession_number}-index.htm"


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _empty_result(ticker: str, company: dict[str, str], cik: str, lookback_days: int, mode: str) -> dict[str, Any]:
    base = {
        "error": False,
        "mode": mode,
        "ticker": ticker,
        "company_name": company.get("name"),
        "cik": cik,
        "lookback_days": lookback_days,
        "transaction_count": 0,
        "transactions": [],
        "narrative": (
            f"No Form 4 insider transactions found for {company.get('name') or ticker} "
            f"in the last {lookback_days} days."
        ),
    }
    if mode == "risk":
        base.update(
            {
                "risk_score": 0.0,
                "risk_level": score_to_risk_level(0.0),
                "open_market_buy_count": 0,
                "open_market_sell_count": 0,
                "distinct_insiders_selling": 0,
            }
        )
    return base


def _build_summary_response(
    ticker: str, company: dict[str, str], cik: str, lookback_days: int, transactions: list[dict[str, Any]]
) -> dict[str, Any]:
    transactions_sorted = sorted(transactions, key=lambda t: t.get("transaction_date") or "", reverse=True)

    plain = [
        {
            "transaction_date": t["transaction_date"],
            "filing_date": t["filing_date"],
            "owner_name": t["owner_name"],
            "owner_titles": t["owner_titles"],
            "transaction_code": t["transaction_code"],
            "transaction_type": t["transaction_code_label"],
            "shares": t["shares"],
            "price_per_share": t["price_per_share"],
            "estimated_value": t["estimated_value"],
            "acquired_or_disposed": "Acquired" if t["acquired_or_disposed"] == "A" else "Disposed" if t["acquired_or_disposed"] == "D" else None,
            "filing_url": t["filing_url"],
        }
        for t in transactions_sorted
    ]

    return {
        "error": False,
        "mode": "summary",
        "ticker": ticker,
        "company_name": company.get("name"),
        "cik": cik,
        "lookback_days": lookback_days,
        "transaction_count": len(plain),
        "transactions": plain,
        "narrative": (
            f"{company.get('name') or ticker} had {len(plain)} insider transaction(s) "
            f"reported on Form 4 in the last {lookback_days} days."
        ),
    }


def _build_risk_response(
    ticker: str, company: dict[str, str], cik: str, lookback_days: int, transactions: list[dict[str, Any]]
) -> dict[str, Any]:
    open_market = [t for t in transactions if t["is_open_market"]]
    buys = [t for t in open_market if t["acquired_or_disposed"] == "A"]
    sells = [t for t in open_market if t["acquired_or_disposed"] == "D"]

    distinct_sellers = {t["owner_name"] for t in sells}
    distinct_buyers = {t["owner_name"] for t in buys}

    total_sell_value = sum(t["estimated_value"] for t in sells if t["estimated_value"])
    total_buy_value = sum(t["estimated_value"] for t in buys if t["estimated_value"])

    officer_or_director_sells = [t for t in sells if t["owner_is_officer"] or t["owner_is_director"]]

    cluster_score = min(5.0, len(distinct_sellers) * 1.2)
    officer_weight = min(3.0, len(officer_or_director_sells) * 0.8)
    value_weight = 2.0 if total_sell_value > 5_000_000 else (1.0 if total_sell_value > 1_000_000 else 0.0)
    buy_offset = min(4.0, len(distinct_buyers) * 1.5)

    raw_score = cluster_score + officer_weight + value_weight - buy_offset
    normalized_score = max(0.0, min(10.0, raw_score))
    risk_level = score_to_risk_level(normalized_score)

    transactions_sorted = sorted(transactions, key=lambda t: t.get("transaction_date") or "", reverse=True)
    flagged = [
        {
            "transaction_date": t["transaction_date"],
            "owner_name": t["owner_name"],
            "owner_titles": t["owner_titles"],
            "transaction_type": t["transaction_code_label"],
            "shares": t["shares"],
            "estimated_value": t["estimated_value"],
            "filing_url": t["filing_url"],
        }
        for t in transactions_sorted
        if t["is_open_market"] and t["acquired_or_disposed"] == "D" and (t["owner_is_officer"] or t["owner_is_director"])
    ]

    narrative = _risk_narrative(
        company.get("name") or ticker,
        lookback_days,
        distinct_sellers,
        distinct_buyers,
        total_sell_value,
        total_buy_value,
        officer_or_director_sells,
        risk_level,
    )

    return {
        "error": False,
        "mode": "risk",
        "ticker": ticker,
        "company_name": company.get("name"),
        "cik": cik,
        "lookback_days": lookback_days,
        "transaction_count": len(transactions),
        "risk_score": round(normalized_score, 1),
        "risk_level": risk_level,
        "open_market_buy_count": len(buys),
        "open_market_sell_count": len(sells),
        "distinct_insiders_selling": len(distinct_sellers),
        "distinct_insiders_buying": len(distinct_buyers),
        "total_open_market_sell_value": round(total_sell_value, 2),
        "total_open_market_buy_value": round(total_buy_value, 2),
        "officer_or_director_sell_count": len(officer_or_director_sells),
        "flagged_transactions": flagged,
        "all_transactions": [
            {
                "transaction_date": t["transaction_date"],
                "owner_name": t["owner_name"],
                "transaction_code": t["transaction_code"],
                "transaction_type": t["transaction_code_label"],
                "shares": t["shares"],
                "estimated_value": t["estimated_value"],
                "filing_url": t["filing_url"],
            }
            for t in transactions_sorted
        ],
        "narrative": narrative,
        "disclaimer": (
            "This analysis is derived solely from SEC Form 4 filings and is not "
            "investment advice. Insider selling has many routine, non-predictive "
            "explanations (diversification, tax planning, pre-set 10b5-1 plans). "
            "Open-market buying is generally considered a stronger conviction "
            "signal than selling is a negative one."
        ),
    }


def _risk_narrative(
    company_name: str,
    lookback_days: int,
    distinct_sellers: set,
    distinct_buyers: set,
    total_sell_value: float,
    total_buy_value: float,
    officer_or_director_sells: list[dict[str, Any]],
    risk_level: str,
) -> str:
    if not distinct_sellers and not distinct_buyers:
        return (
            f"No open-market insider buying or selling at {company_name} in the last "
            f"{lookback_days} days. Any Form 4 activity found was compensation-driven "
            "(grants, option exercises, tax withholding) rather than open-market conviction trades."
        )

    parts = [f"Over the last {lookback_days} days, {company_name} insiders show {risk_level} risk signal."]

    if distinct_sellers:
        parts.append(
            f"{len(distinct_sellers)} distinct insider(s) sold on the open market "
            f"(~${total_sell_value:,.0f} total)."
        )
    if officer_or_director_sells:
        parts.append(
            f"{len(officer_or_director_sells)} of these sales were by officers or directors — "
            "the more closely watched category of insider."
        )
    if distinct_buyers:
        parts.append(
            f"{len(distinct_buyers)} distinct insider(s) bought on the open market "
            f"(~${total_buy_value:,.0f} total), which is generally read as a positive conviction signal "
            "and offsets some of the selling-driven risk."
        )
    elif distinct_sellers:
        parts.append("No offsetting open-market insider buying was found in this window.")

    return " ".join(parts)