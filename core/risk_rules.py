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