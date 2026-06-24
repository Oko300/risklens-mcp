"""
core/risk_rules.py
====================
Shared, declarative risk taxonomy used by both tools. Keeping this in one
module means the risk scoring logic is consistent and auditable, and is not
duplicated/drifted between eight_k_events.py and insider_activity.py.

This is NOT investment advice — it's a structured way of flagging which
parts of a public filing are conventionally treated as higher-risk
disclosure events, based on the SEC's own Form 8-K item categories and
Form 4 transaction codes.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Form 8-K item codes -> (label, risk_weight, risk_category)
# risk_weight: 0 (routine) .. 3 (severe)
# ---------------------------------------------------------------------------

EIGHT_K_ITEMS: dict[str, dict[str, object]] = {
    "1.01": {"label": "Entry into a Material Definitive Agreement", "weight": 1, "category": "corporate_action"},
    "1.02": {"label": "Termination of a Material Definitive Agreement", "weight": 2, "category": "corporate_action"},
    "1.03": {"label": "Bankruptcy or Receivership", "weight": 3, "category": "solvency"},
    "1.04": {"label": "Mine Safety - Reporting of Shutdowns and Patterns of Violations", "weight": 1, "category": "operational"},
    "2.01": {"label": "Completion of Acquisition or Disposition of Assets", "weight": 1, "category": "corporate_action"},
    "2.02": {"label": "Results of Operations and Financial Condition", "weight": 0, "category": "earnings"},
    "2.03": {"label": "Creation of a Direct Financial Obligation / Off-Balance Sheet Arrangement", "weight": 2, "category": "financial_obligation"},
    "2.04": {"label": "Triggering Events That Accelerate or Increase a Financial Obligation", "weight": 3, "category": "financial_obligation"},
    "2.05": {"label": "Costs Associated with Exit or Disposal Activities", "weight": 2, "category": "operational"},
    "2.06": {"label": "Material Impairments", "weight": 3, "category": "financial_health"},
    "3.01": {"label": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule", "weight": 3, "category": "listing_risk"},
    "3.02": {"label": "Unregistered Sales of Equity Securities", "weight": 1, "category": "capital_structure"},
    "3.03": {"label": "Material Modification to Rights of Security Holders", "weight": 2, "category": "capital_structure"},
    "4.01": {"label": "Changes in Registrant's Certifying Accountant", "weight": 2, "category": "governance"},
    "4.02": {"label": "Non-Reliance on Previously Issued Financial Statements (Restatement)", "weight": 3, "category": "accounting_integrity"},
    "5.01": {"label": "Changes in Control of Registrant", "weight": 2, "category": "governance"},
    "5.02": {"label": "Departure/Election of Directors or Principal Officers", "weight": 1, "category": "governance"},
    "5.03": {"label": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year", "weight": 0, "category": "governance"},
    "5.04": {"label": "Temporary Suspension of Trading Under Employee Benefit Plans", "weight": 1, "category": "governance"},
    "5.05": {"label": "Amendments to Code of Ethics, or Waiver of a Provision", "weight": 1, "category": "governance"},
    "5.06": {"label": "Change in Shell Company Status", "weight": 1, "category": "corporate_action"},
    "5.07": {"label": "Submission of Matters to a Vote of Security Holders", "weight": 0, "category": "governance"},
    "5.08": {"label": "Shareholder Director Nominations", "weight": 0, "category": "governance"},
    "6.01": {"label": "ABS Informational and Computational Material", "weight": 0, "category": "asset_backed"},
    "6.02": {"label": "Change of Servicer or Trustee", "weight": 1, "category": "asset_backed"},
    "6.03": {"label": "Change in Credit Enhancement or Other External Support", "weight": 2, "category": "asset_backed"},
    "6.04": {"label": "Failure to Make a Required Distribution", "weight": 3, "category": "asset_backed"},
    "6.05": {"label": "Securities Act Updating Disclosure", "weight": 0, "category": "asset_backed"},
    "7.01": {"label": "Regulation FD Disclosure", "weight": 0, "category": "disclosure"},
    "8.01": {"label": "Other Events", "weight": 0, "category": "other"},
    "9.01": {"label": "Financial Statements and Exhibits", "weight": 0, "category": "exhibits"},
}

# Item codes that, on their own, represent a clear elevated-risk signal.
HIGH_RISK_8K_ITEMS = {code for code, meta in EIGHT_K_ITEMS.items() if meta["weight"] >= 2}
SEVERE_RISK_8K_ITEMS = {code for code, meta in EIGHT_K_ITEMS.items() if meta["weight"] >= 3}


def classify_8k_item(item_code: str) -> dict[str, object]:
    """Return taxonomy metadata for a single 8-K item code, e.g. '4.02'."""
    item_code = item_code.strip()
    meta = EIGHT_K_ITEMS.get(item_code)
    if meta is None:
        return {"code": item_code, "label": "Unrecognized/Other Item", "weight": 0, "category": "unknown"}
    return {"code": item_code, **meta}


# ---------------------------------------------------------------------------
# Form 4 transaction codes
# ---------------------------------------------------------------------------

FORM4_TRANSACTION_CODES: dict[str, str] = {
    "P": "Open market purchase",
    "S": "Open market sale",
    "A": "Grant, award, or other acquisition (e.g. equity comp)",
    "D": "Sale to issuer (e.g. cashless exercise withholding)",
    "F": "Tax-withholding disposition (shares withheld for taxes)",
    "M": "Exercise or conversion of derivative security",
    "G": "Bona fide gift",
    "V": "Transaction voluntarily reported earlier than required",
    "C": "Conversion of derivative security",
    "E": "Expiration of short derivative position",
    "H": "Expiration of long derivative position",
    "I": "Discretionary transaction (Rule 16b-3(f))",
    "J": "Other acquisition or disposition (footnote required)",
    "K": "Equity swap or similar transaction",
    "L": "Small acquisition under Rule 16a-6",
    "O": "Exercise of out-of-the-money derivative",
    "U": "Disposition pursuant to a tender of shares in a change of control",
    "W": "Acquisition or disposition by will or laws of descent",
    "X": "Exercise of in-the-money or at-the-money derivative",
    "Z": "Deposit into or withdrawal from a voting trust",
}

# Codes that represent genuine open-market conviction trades (the signal
# investors actually care about) vs. mechanical/compensation-driven codes.
OPEN_MARKET_CODES = {"P", "S"}
COMPENSATION_DRIVEN_CODES = {"A", "M", "F", "X", "C"}


def classify_form4_code(code: str) -> dict[str, object]:
    code = code.strip().upper()
    label = FORM4_TRANSACTION_CODES.get(code, "Unrecognized transaction code")
    return {
        "code": code,
        "label": label,
        "is_open_market": code in OPEN_MARKET_CODES,
        "is_compensation_driven": code in COMPENSATION_DRIVEN_CODES,
    }


# ---------------------------------------------------------------------------
# Risk level bucketing (shared scoring -> human label)
# ---------------------------------------------------------------------------


def score_to_risk_level(score: float) -> str:
    """Map a normalized 0-10 risk score to a human-readable risk level."""
    if score >= 7.5:
        return "SEVERE"
    if score >= 5.0:
        return "ELEVATED"
    if score >= 2.5:
        return "MODERATE"
    return "LOW"


# ---------------------------------------------------------------------------
# Content-based severity scoring
# ---------------------------------------------------------------------------
# Item codes where the *base* weight from item code alone is insufficient —
# e.g. 5.02 covers everything from "junior VP retires" to "CEO stepping down",
# and those are not remotely equivalent in risk. For these codes, we scan the
# already-extracted filing text (which we now have via filing_content.py) for
# role-level keywords to determine the actual severity.
#
# This runs on text we already fetched — no extra API calls, no latency hit.
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402

# Roles that, when detected as the departing/appointed person's title in a
# 5.02 filing, indicate a genuinely high-significance leadership event.
# Ordered from most to least significant so we can report the highest match.
_C_SUITE_TITLES: list[tuple[str, int]] = [
    # (display_label, escalated_weight)
    ("Chief Executive Officer", 3),
    ("CEO", 3),
    ("President and Chief Executive", 3),
    ("Chief Financial Officer", 2),
    ("CFO", 2),
    ("Chief Operating Officer", 2),
    ("COO", 2),
    ("Executive Chairman", 2),
    ("Executive Chair", 2),
    ("President", 2),
    ("Chief Technology Officer", 2),
    ("CTO", 2),
    ("Chief Revenue Officer", 2),
    ("Chief Legal Officer", 2),
    ("General Counsel", 2),
]

# Build a single compiled regex for fast matching.
# \b word boundaries are CRITICAL — without them, "COO" matches inside "Cook",
# "CTO" matches inside "director", and so on.
_C_SUITE_PATTERN = _re.compile(
    r"\b(?:" + "|".join(_re.escape(title) for title, _ in _C_SUITE_TITLES) + r")\b",
    _re.IGNORECASE,
)

# Map title -> escalated_weight for fast lookup after a regex match
_TITLE_TO_WEIGHT: dict[str, int] = {title.lower(): w for title, w in _C_SUITE_TITLES}


def score_5_02_from_text(text: str) -> dict[str, object]:
    """
    Given the extracted text of a 5.02 filing section, return a content-aware
    severity assessment:
        {
            "escalated_weight": int,      # upgraded weight (1-3)
            "detected_title": str | None, # the specific role that triggered escalation
            "content_risk_label": str,    # human-readable reason
        }

    Returns base weight=1 with no detected title if no high-significance
    title is found — safe fallback, no false positives.
    """
    if not text:
        return {"escalated_weight": 1, "detected_title": None, "content_risk_label": "Officer/director change — role significance unknown"}

    match = _C_SUITE_PATTERN.search(text)
    if not match:
        return {"escalated_weight": 1, "detected_title": None, "content_risk_label": "Officer/director change — role not identified as C-suite"}

    matched_title = match.group(0)
    escalated = _TITLE_TO_WEIGHT.get(matched_title.lower(), 1)
    label = f"{'CEO' if escalated == 3 else 'Senior executive'} transition — {matched_title} named in filing"
    return {
        "escalated_weight": escalated,
        "detected_title": matched_title,
        "content_risk_label": label,
    }


# Item codes where content-based scoring applies (currently just 5.02,
# but structured so other codes can be added in future without touching
# the calling code in eight_k_events.py).
CONTENT_SCORED_ITEMS = {"5.02"}

# can't capture on its own. A single 5.02 (officer departure) is routine;
# three of them in 60 days is a pattern worth flagging explicitly, with the
# reason stated plainly rather than buried in a generic risk_score.
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

# (item_code, cluster_window_days, min_occurrences, escalated_label, bump)
CLUSTER_RULES: list[dict[str, object]] = [
    {
        "item_codes": {"5.02"},
        "window_days": 60,
        "min_count": 2,
        "reason_template": "{count} executive/director departure filings (Item 5.02) within {window} days",
        "score_bump": 2.5,
    },
    {
        "item_codes": {"5.02"},
        "window_days": 30,
        "min_count": 3,
        "reason_template": "{count} executive/director departure filings (Item 5.02) within {window} days — rapid leadership turnover",
        "score_bump": 4.0,
    },
    {
        "item_codes": {"4.01"},
        "window_days": 365,
        "min_count": 2,
        "reason_template": "{count} auditor changes (Item 4.01) within {window} days — unusual audit-relationship instability",
        "score_bump": 3.0,
    },
    {
        "item_codes": {"2.04"},
        "window_days": 180,
        "min_count": 2,
        "reason_template": "{count} debt-acceleration triggering events (Item 2.04) within {window} days",
        "score_bump": 3.0,
    },
    {
        "item_codes": {"8.01"},
        "window_days": 30,
        "min_count": 4,
        "reason_template": "{count} 'Other Events' filings (Item 8.01) within {window} days — unusually high disclosure cadence worth a closer look",
        "score_bump": 1.5,
    },
    {
        "item_codes": {"1.02", "2.04", "2.05", "2.06", "3.01"},
        "window_days": 180,
        "min_count": 2,
        "reason_template": "{count} distinct financial-distress-pattern filings (contract terminations, accelerated debt, impairments, or delisting notices) within {window} days",
        "score_bump": 4.0,
    },
]


def detect_clusters(filings: list[dict[str, object]]) -> list[dict[str, object]]:
    """
    Scan a list of filings (each with "filing_date": "YYYY-MM-DD" and
    "item_codes": list[str]) for clustering patterns defined in CLUSTER_RULES.

    Returns a list of triggered clusters:
        [{"reason": str, "score_bump": float, "item_codes": [...], "matched_dates": [...]}]

    This is what turns "two 5.02 filings, neither individually severe" into
    an explicit, explainable HIGH-risk reason instead of staying invisible
    inside a flat sum-of-weights score.

    Rules targeting the SAME item-code set are mutually exclusive — only the
    single strongest (highest score_bump) matching rule per item-code-set
    fires. This prevents a tighter/stronger pattern (e.g. 3 filings in 30
    days) from stacking its bump on top of a looser version of the same
    pattern (e.g. 2 filings in 60 days), which would double-count one
    underlying fact pattern as two separate risk signals.
    """
    parsed: list[tuple] = []
    for f in filings:
        try:
            d = datetime.strptime(str(f["filing_date"]), "%Y-%m-%d").date()
        except (ValueError, KeyError, TypeError):
            continue
        codes = set(f.get("item_codes", []))
        parsed.append((d, codes))

    # Group rules by their target item-code set so we can pick only the
    # strongest match within each group.
    candidates_by_group: dict[frozenset, list[dict[str, object]]] = {}

    for rule in CLUSTER_RULES:
        target_codes = rule["item_codes"]
        window = int(rule["window_days"])
        min_count = int(rule["min_count"])

        matches = [(d, codes & target_codes) for d, codes in parsed if codes & target_codes]
        if len(matches) < min_count:
            continue

        matches.sort(key=lambda x: x[0])
        for i in range(len(matches) - min_count + 1):
            window_slice = matches[i : i + min_count]
            span = (window_slice[-1][0] - window_slice[0][0]).days
            if span <= window:
                matched_dates = [d.isoformat() for d, _ in window_slice]
                matched_codes = sorted({c for _, codes in window_slice for c in codes})
                group_key = frozenset(target_codes)
                candidates_by_group.setdefault(group_key, []).append(
                    {
                        "reason": rule["reason_template"].format(count=len(window_slice), window=window),
                        "score_bump": float(rule["score_bump"]),
                        "item_codes": matched_codes,
                        "matched_dates": matched_dates,
                    }
                )
                break  # one trigger per rule is enough; avoid double-counting overlapping windows within the same rule

    # Keep only the strongest (highest score_bump) candidate per group.
    triggered = []
    for group_key, candidates in candidates_by_group.items():
        best = max(candidates, key=lambda c: c["score_bump"])
        triggered.append(best)

    return triggered


def risk_tier_with_reason(score: float, cluster_reasons: list[str], severe_labels: list[str]) -> dict[str, str]:
    """
    Produce a tier + a single, concrete, human-readable reason string —
    e.g. "HIGH - Multiple key executive departures in 30 days" — instead of
    a bare LOW/MODERATE/ELEVATED/SEVERE label with no explanation.
    """
    level = score_to_risk_level(score)

    if severe_labels:
        reason = severe_labels[0]
    elif cluster_reasons:
        reason = cluster_reasons[0]
    elif score > 0:
        reason = "Elevated-risk disclosure category present, but isolated (no clustering or severe pattern detected)"
    else:
        reason = "No risk-flagged disclosure categories in this window"

    return {"level": level, "reason": reason, "tier_label": f"{level} - {reason}"}