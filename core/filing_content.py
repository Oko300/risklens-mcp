"""
core/filing_content.py
========================
Extracts real, readable content from the body of an 8-K filing — not just
its item codes. This is what powers the "details" field on every filing
in analyze_8k_events, and the fuller text on flagged/high-risk filings.

Design:
  - 8-K bodies are HTML documents organized under "Item X.XX" headers
    (e.g. "Item 5.02 Departure of Directors..."). We fetch the primary
    document, strip it to clean text, then split it into sections keyed
    by item code using the same numbering scheme as core/risk_rules.py.
  - For each item code a filing discloses, we return:
      - a short excerpt (~100-120 words) for fast/default display
      - the full section text, used only when the caller asks for it
        (reserved for flagged/high-risk filings, to keep routine calls fast)
  - All fetching goes through core/sec_client.py so rate limiting and the
    User-Agent header stay centralized.
  - Every failure mode (missing document, fetch error, no recognizable
    item structure, exhibit-only filings with no body text) degrades to
    an empty/None result rather than raising — callers always get a
    response, just without the content extension when it's unavailable.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from core.sec_client import fetch_document_text

logger = logging.getLogger("risklens.filing_content")

# Matches headers like "Item 5.02", "Item 5.02.", "ITEM 5.02 —", "Item 5.02:"
# at the start of a line/block, capturing the item code for section splitting.
_ITEM_HEADER_RE = re.compile(
    r"item\s+(\d{1,2}\.\d{2})\b[\s.:\u2013\u2014-]*",
    re.IGNORECASE,
)

# Boilerplate phrases that show up at the start/end of nearly every 8-K and
# add no real signal — stripped out of excerpts so the excerpt is actually
# about the disclosed event, not the filing's legal preamble.
_BOILERPLATE_PATTERNS = [
    re.compile(r"united states\s+securities and exchange commission.*?form 8-k", re.IGNORECASE | re.DOTALL),
    re.compile(r"current report.*?pursuant to section 13.*?exchange act of 1934", re.IGNORECASE | re.DOTALL),
    re.compile(r"check the appropriate box.*?of the chapter\)", re.IGNORECASE | re.DOTALL),
    # Bounded to a single line/short span so this can never cross into an
    # "Item X.XX" section further down the document (a DOTALL ".*?" here
    # previously matched all the way to an unrelated "exchange act" phrase
    # inside the signature block, silently deleting the entire filing body).
    re.compile(r"indicate by check mark whether the registrant is an emerging growth company[^\n]{0,250}", re.IGNORECASE),
    re.compile(r"emerging growth company\.?(?:[^\n]{0,200}exchange act\.?)?", re.IGNORECASE),
    re.compile(r"pursuant to the requirements of the securities exchange act.*?duly authorized\.?", re.IGNORECASE | re.DOTALL),
]

DEFAULT_EXCERPT_TARGET_WORDS = 110
MAX_EXCERPT_WORDS = 140
MIN_EXCERPT_WORDS = 60


def html_to_clean_text(html: str) -> str:
    """Strip HTML markup down to readable plain text, collapsing whitespace."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines and excess whitespace from table-heavy
    # EDGAR markup, while preserving paragraph breaks.
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def strip_boilerplate(text: str) -> str:
    cleaned = text
    for pattern in _BOILERPLATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def split_into_item_sections(clean_text: str) -> dict[str, str]:
    """
    Split a cleaned 8-K body into {item_code: section_text}, using "Item X.XX"
    headers as section boundaries. Text before the first item header (the
    cover page / signature block boilerplate) is discarded.
    """
    matches = list(_ITEM_HEADER_RE.finditer(clean_text))
    if not matches:
        return {}

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        item_code = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(clean_text)
        section_text = clean_text[start:end].strip()
        if not section_text:
            continue

        # The first "sentence" right after "Item X.XX" is almost always the
        # SEC's own boilerplate item title (e.g. "Departure of Directors or
        # Certain Officers; Election of Directors..."), which just restates
        # the item_label we already surface elsewhere. Drop it so the
        # section leads with the company's actual disclosure, not a label.
        section_text = _strip_leading_item_title(section_text)
        if not section_text:
            continue

        # Some filings repeat an item header in a table of contents AND in
        # the body. Keep the longer occurrence (the real content), since a
        # short stub is almost always just a ToC line catching a regex hit.
        if item_code not in sections or len(section_text) > len(sections[item_code]):
            sections[item_code] = section_text

    return sections


def _strip_leading_item_title(section_text: str) -> str:
    """
    Remove a leading item-title line/sentence that just restates the item's
    official SEC name (ending in a period), so the section starts at the
    actual disclosed content. Conservative: only strips if the text up to
    the first period looks like a title (short, no digits suggesting a date
    or dollar figure already in play) — otherwise leaves the text as-is.
    """
    # Title may end with ". " (inline) or ".\n" (own line) — find whichever
    # comes first.
    candidates = [i for i in (section_text.find(". "), section_text.find(".\n")) if i != -1]
    if not candidates:
        return section_text
    first_period = min(candidates)
    if first_period > 200:
        return section_text
    candidate_title = section_text[:first_period]
    # Titles are label-like: no digits, reasonably short.
    if any(ch.isdigit() for ch in candidate_title):
        return section_text
    return section_text[first_period + 1:].lstrip()


def _truncate_to_words(text: str, target_words: int, max_words: int) -> str:
    words = text.split()
    if len(words) <= target_words:
        return text.strip()

    truncated = " ".join(words[:max_words])
    # Try to end on a sentence boundary near the target so the excerpt
    # reads naturally rather than cutting off mid-sentence.
    sentence_end = max(truncated.rfind(". "), truncated.rfind(".\n"))
    if sentence_end > len(" ".join(words[:MIN_EXCERPT_WORDS])):
        return truncated[: sentence_end + 1].strip()
    return truncated.strip() + "…"


def build_excerpt(section_text: str) -> str:
    """Produce a clear, scannable excerpt: not a one-liner, not a wall of text."""
    if not section_text:
        return ""
    return _truncate_to_words(section_text, DEFAULT_EXCERPT_TARGET_WORDS, MAX_EXCERPT_WORDS)


async def fetch_item_sections(cik: str, accession_number: str, primary_document: Optional[str]) -> dict[str, str]:
    """
    Fetch and parse the actual 8-K body document, returning
    {item_code: full_section_text} for every item disclosed in the filing.

    Returns {} (not an exception) on any fetch/parse failure — callers
    should treat that as "content unavailable" and fall back to item-code
    labels only, never let this break the surrounding risk analysis.
    """
    if not primary_document:
        return {}

    try:
        html = await fetch_document_text(cik, accession_number, primary_document)
        if not html:
            return {}

        clean = html_to_clean_text(html)
        clean = strip_boilerplate(clean)
        sections = split_into_item_sections(clean)
        return sections
    except Exception:
        logger.exception(
            "Failed to fetch/parse 8-K body for cik=%s accession=%s doc=%s",
            cik, accession_number, primary_document,
        )
        return {}