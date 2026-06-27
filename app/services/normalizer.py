"""
Transaction Normalization Engine.

Takes raw GCC bank statement description strings — messy, bilingual,
truncated, full of POS terminal noise and vendor registration numbers —
and maps them to clean analytical categories with a confidence score.

Pipeline per transaction:
  1. Pre-process  — strip noise, normalize whitespace, uppercase ASCII
  2. Rule match   — ordered regex rules, direction-aware, first match wins
  3. Fuzzy match  — rapidfuzz against merchant keyword lists for leftovers
  4. Fallback     — UNCATEGORIZED with low confidence

No ML models. No external calls. Pure deterministic logic that can be
audited, explained, and updated rule-by-rule as new GCC patterns emerge.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from app.models.transaction import TransactionCategory, TransactionDirection


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class NormalizationResult:
    category: TransactionCategory
    normalized_description: str
    confidence: float          # 0.0 – 1.0
    matched_rule: str          # name of the rule that fired, for audit logs


# ---------------------------------------------------------------------------
# Rule definition
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    category: TransactionCategory
    confidence: float
    direction_filter: Optional[TransactionDirection] = None  # None = any


# ---------------------------------------------------------------------------
# Noise stripping patterns
# Removes reference numbers, card digits, POS terminal IDs etc.
# We keep the merchant/counterparty name and drop the noise.
# ---------------------------------------------------------------------------

_NOISE_PATTERNS = [
    re.compile(r'\b\d{6,}\b'),                    # 6+ digit ref numbers
    re.compile(r'\*{2,4}\d{4}'),                  # ****1234 card fragments
    re.compile(r'\bREF:?[\w\d]+\b', re.I),        # REF:12345 or REF 12345
    re.compile(r'\bTXN:[\w\d]+\b', re.I),         # TXN:12345
    re.compile(r'\bMCC:\d{4}\b', re.I),           # MCC:5411
    re.compile(r'\bCR\s+[\d.]+\b'),               # CR 2345.000 (amount suffix)
    re.compile(r'\s{2,}'),                         # multiple whitespace → single
]


def _strip_noise(text: str) -> str:
    result = text.upper().strip()
    for pat in _NOISE_PATTERNS:
        result = pat.sub(' ', result)
    return result.strip()


# ---------------------------------------------------------------------------
# Ordered rule set — direction-aware, first match wins
#
# Confidence tiers:
#   0.95  exact known pattern (SADAD, WPS, LMRA)
#   0.85  strong keyword match
#   0.75  moderate keyword match
#   0.60  fuzzy / inferred
# ---------------------------------------------------------------------------

RULES: list[Rule] = [

    # ── PAYROLL ─────────────────────────────────────────────────────────────
    Rule("wps_salary",
         re.compile(r'\bWPS\b.*\b(SALARY|SALARIES|رواتب)\b', re.I | re.UNICODE),
         TransactionCategory.PAYROLL, 0.97,
         TransactionDirection.DEBIT),

    Rule("payroll_transfer",
         re.compile(r'\bPAYROLL\b', re.I),
         TransactionCategory.PAYROLL, 0.95,
         TransactionDirection.DEBIT),

    Rule("salary_keyword",
         re.compile(r'\b(SALARY|SALARIES|رواتب|رواتب)\b', re.I | re.UNICODE),
         TransactionCategory.PAYROLL, 0.90,
         TransactionDirection.DEBIT),

    Rule("sif_payroll",
         re.compile(r'\bSIF\s+PAYROLL\b', re.I),
         TransactionCategory.PAYROLL, 0.95,
         TransactionDirection.DEBIT),

    # ── GOVERNMENT / VAT ────────────────────────────────────────────────────
    Rule("sadad_payment",
         re.compile(r'\bSADAD\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("lmra_levy",
         re.compile(r'\bLMRA\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("vat_payment",
         re.compile(r'\bVAT\b.*(PAYMENT|RETURN|PMT)\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.95,
         TransactionDirection.DEBIT),

    Rule("mof_payment",
         re.compile(r'\bMOF\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.93,
         TransactionDirection.DEBIT),

    Rule("tamkeen",
         re.compile(r'\bTAMKEEN\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("gosi",
         re.compile(r'\bGOSI\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("sijilat",
         re.compile(r'\bSIJILAT\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("customs_duty",
         re.compile(r'\bCUSTOMS\s+DUTY\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.95,
         TransactionDirection.DEBIT),

    Rule("modon_fees",
         re.compile(r'\bMODON\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.95,
         TransactionDirection.DEBIT),

    Rule("crpd_fees",
         re.compile(r'\bCRPD\b', re.I),
         TransactionCategory.GOVERNMENT_VAT, 0.93,
         TransactionDirection.DEBIT),

    Rule("arabic_vat",
         re.compile(r'ضريبة\s*القيمة\s*المضافة', re.UNICODE),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    Rule("arabic_expat_levy",
         re.compile(r'رسوم\s*العمالة', re.UNICODE),
         TransactionCategory.GOVERNMENT_VAT, 0.97,
         TransactionDirection.DEBIT),

    # ── COMMERCIAL RENT ─────────────────────────────────────────────────────
    Rule("ijarah",
         re.compile(r'\bIJARAH\b', re.I),
         TransactionCategory.COMMERCIAL_RENT, 0.97,
         TransactionDirection.DEBIT),

    Rule("monthly_rent",
         re.compile(r'\b(MONTHLY\s+RENT|COMMERCIAL\s+RENT)\b', re.I),
         TransactionCategory.COMMERCIAL_RENT, 0.95,
         TransactionDirection.DEBIT),

    Rule("property_payment",
         re.compile(r'\bPROPERTY\s+PMT\b', re.I),
         TransactionCategory.COMMERCIAL_RENT, 0.90,
         TransactionDirection.DEBIT),

    Rule("lease_payment",
         re.compile(r'\bLEASE\s+(PMT|PAYMENT)\b', re.I),
         TransactionCategory.COMMERCIAL_RENT, 0.90,
         TransactionDirection.DEBIT),

    Rule("arabic_rent",
         re.compile(r'\bإيجار\b', re.UNICODE),
         TransactionCategory.COMMERCIAL_RENT, 0.95,
         TransactionDirection.DEBIT),

    # ── BANK CHARGES ────────────────────────────────────────────────────────
    Rule("maintenance_fee",
         re.compile(r'\b(ACCOUNT\s+MAINTENANCE|SERVICE\s+CHARGE)\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.97,
         TransactionDirection.DEBIT),

    Rule("swift_fee",
         re.compile(r'\bSWIFT\s+(TRANSFER\s+)?FEE\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.97,
         TransactionDirection.DEBIT),

    Rule("sms_charges",
         re.compile(r'\bSMS\s+(ALERT\s+)?CHARGES?\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.97,
         TransactionDirection.DEBIT),

    Rule("chequebook_fee",
         re.compile(r'\bCHEQUEBOOK\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.97,
         TransactionDirection.DEBIT),

    Rule("card_annual_fee",
         re.compile(r'\bCARD\s+ANNUAL\s+FEE\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.97,
         TransactionDirection.DEBIT),

    Rule("atm_fee",
         re.compile(r'\bATM\s+(USAGE\s+)?FEE\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.95,
         TransactionDirection.DEBIT),

    Rule("overdraft_interest",
         re.compile(r'\bOVERDRAFT\s+INTEREST\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.95,
         TransactionDirection.DEBIT),

    Rule("statement_fee",
         re.compile(r'\bSTATEMENT\s+FEE\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.95,
         TransactionDirection.DEBIT),

    Rule("intl_transfer_charge",
         re.compile(r'\bINTERNATIONAL\s+TRANSFER\s+CHARGE\b', re.I),
         TransactionCategory.BANK_CHARGES, 0.95,
         TransactionDirection.DEBIT),

    # ── INTER-ACCOUNT TRANSFERS ──────────────────────────────────────────────
    Rule("own_account_transfer",
         re.compile(r'\b(OWN\s+ACCOUNT|SAME\s+ENTITY|INTERNAL\s+TRF)\b', re.I),
         TransactionCategory.INTER_ACCOUNT, 0.95),

    Rule("petty_cash",
         re.compile(r'\bPETTY\s+CASH\b', re.I),
         TransactionCategory.INTER_ACCOUNT, 0.95,
         TransactionDirection.DEBIT),

    Rule("fd_account",
         re.compile(r'\b(TO\s+FD|TO\s+SAVINGS)\s+ACCOUNT\b', re.I),
         TransactionCategory.INTER_ACCOUNT, 0.90,
         TransactionDirection.DEBIT),

    Rule("arabic_internal",
         re.compile(r'تحويل\s*داخلي', re.UNICODE),
         TransactionCategory.INTER_ACCOUNT, 0.95),

    # ── PRIMARY REVENUE (credits only) ──────────────────────────────────────
    Rule("inward_remittance",
         re.compile(r'\bINWARD\s+REMITTANCE\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.93,
         TransactionDirection.CREDIT),

    Rule("benefit_pay_received",
         re.compile(r'\bBENEFIT\s+PAY\s+RECEIVED\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.97,
         TransactionDirection.CREDIT),

    Rule("neft_credit",
         re.compile(r'\bNEFT\s+CR\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.93,
         TransactionDirection.CREDIT),

    Rule("sales_settlement",
         re.compile(r'\bSALES\s+SETTLEMENT\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("batch_credit",
         re.compile(r'\bBATCH\s+CREDIT\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("swift_credit",
         re.compile(r'\bSWIFT\s+CR\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("ibg_credit",
         re.compile(r'\bIBG\s+CREDIT\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("trf_from",
         re.compile(r'\bTRF\s+FROM\b', re.I),
         TransactionCategory.PRIMARY_REVENUE, 0.85,
         TransactionDirection.CREDIT),

    Rule("arabic_incoming_transfer",
         re.compile(r'تحويل\s*(من|وارد)', re.UNICODE),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("arabic_client_payment",
         re.compile(r'دفعة\s*عميل', re.UNICODE),
         TransactionCategory.PRIMARY_REVENUE, 0.90,
         TransactionDirection.CREDIT),

    Rule("arabic_revenue",
         re.compile(r'\bايراد\b', re.UNICODE),
         TransactionCategory.PRIMARY_REVENUE, 0.88,
         TransactionDirection.CREDIT),

    # ── DISCRETIONARY CAPEX ─────────────────────────────────────────────────
    # Known GCC/Bahrain merchants — debit only
    Rule("amazon",
         re.compile(r'\bAMAZON\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("noon",
         re.compile(r'\bNOON\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("carrefour",
         re.compile(r'\bCARREFOUR\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.93,
         TransactionDirection.DEBIT),

    Rule("ikea",
         re.compile(r'\bIKEA\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.97,
         TransactionDirection.DEBIT),

    Rule("sharaf_dg",
         re.compile(r'\bSHARAF\s*DG\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.97,
         TransactionDirection.DEBIT),

    Rule("lulu",
         re.compile(r'\bLULU\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("geant",
         re.compile(r'\bGEANT\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("ace_hardware",
         re.compile(r'\bACE\s+HARDWARE\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("apple_store",
         re.compile(r'\bAPPLE\s+STORE\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("office_supplies",
         re.compile(r'\b(OFFICE\s+DEPOT|ZOOM\s+STATIONERY|STATIONERY)\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.90,
         TransactionDirection.DEBIT),

    Rule("virgin_megastore",
         re.compile(r'\bVIRGIN\s+MEGASTORE\b', re.I),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.95,
         TransactionDirection.DEBIT),

    Rule("arabic_supermarket",
         re.compile(r'سوبر\s*ماركت', re.UNICODE),
         TransactionCategory.DISCRETIONARY_CAPEX, 0.88,
         TransactionDirection.DEBIT),
]


# ---------------------------------------------------------------------------
# Fuzzy merchant keyword lists
# Used as a fallback when no regex rule fires.
# rapidfuzz partial_ratio catches truncated / abbreviated descriptions.
# ---------------------------------------------------------------------------

_FUZZY_MERCHANTS: list[tuple[str, TransactionCategory, float, Optional[TransactionDirection]]] = [
    # (keyword, category, confidence, direction_filter)
    ("SALARY",        TransactionCategory.PAYROLL,              0.75, TransactionDirection.DEBIT),
    ("PAYROLL",       TransactionCategory.PAYROLL,              0.75, TransactionDirection.DEBIT),
    ("RENT",          TransactionCategory.COMMERCIAL_RENT,      0.72, TransactionDirection.DEBIT),
    ("IJARAH",        TransactionCategory.COMMERCIAL_RENT,      0.80, TransactionDirection.DEBIT),
    ("VAT",           TransactionCategory.GOVERNMENT_VAT,       0.75, TransactionDirection.DEBIT),
    ("GOVERNMENT",    TransactionCategory.GOVERNMENT_VAT,       0.70, TransactionDirection.DEBIT),
    ("SERVICE FEE",   TransactionCategory.BANK_CHARGES,         0.72, TransactionDirection.DEBIT),
    ("BANK CHARGE",   TransactionCategory.BANK_CHARGES,         0.72, TransactionDirection.DEBIT),
    ("REMITTANCE",    TransactionCategory.PRIMARY_REVENUE,      0.72, TransactionDirection.CREDIT),
    ("PAYMENT FROM",  TransactionCategory.PRIMARY_REVENUE,      0.70, TransactionDirection.CREDIT),
]

_FUZZY_THRESHOLD = 80  # rapidfuzz score 0–100


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize(
    raw_description: str,
    direction: TransactionDirection,
    amount: Decimal,
) -> NormalizationResult:
    """
    Categorize a single transaction description.

    Args:
        raw_description: The raw text from the bank statement field.
        direction:       CREDIT or DEBIT (affects which rules are eligible).
        amount:          Transaction amount — reserved for future amount-based rules.

    Returns:
        NormalizationResult with category, cleaned description, confidence,
        and the name of the rule that fired.
    """
    cleaned = _strip_noise(raw_description)

    # 1. Regex rule pass
    for rule in RULES:
        if rule.direction_filter and rule.direction_filter != direction:
            continue
        if rule.pattern.search(cleaned):
            return NormalizationResult(
                category=rule.category,
                normalized_description=_clean_description(cleaned),
                confidence=rule.confidence,
                matched_rule=rule.name,
            )

    # 2. Fuzzy fallback
    upper = cleaned.upper()
    best_score = 0
    best_match: Optional[tuple] = None
    for keyword, category, confidence, dir_filter in _FUZZY_MERCHANTS:
        if dir_filter and dir_filter != direction:
            continue
        score = fuzz.partial_ratio(keyword, upper)
        if score >= _FUZZY_THRESHOLD and score > best_score:
            best_score = score
            best_match = (category, confidence * (score / 100))

    if best_match:
        return NormalizationResult(
            category=best_match[0],
            normalized_description=_clean_description(cleaned),
            confidence=round(best_match[1], 3),
            matched_rule="fuzzy_match",
        )

    # 3. Direction-based heuristic for generic credit transfers
    if direction == TransactionDirection.CREDIT and any(
        kw in cleaned for kw in ("TRF", "TRANSFER", "CREDIT", "تحويل")
    ):
        return NormalizationResult(
            category=TransactionCategory.PRIMARY_REVENUE,
            normalized_description=_clean_description(cleaned),
            confidence=0.60,
            matched_rule="heuristic_credit_transfer",
        )

    # 4. Fallback
    return NormalizationResult(
        category=TransactionCategory.UNCATEGORIZED,
        normalized_description=_clean_description(cleaned),
        confidence=0.0,
        matched_rule="no_match",
    )


def batch_normalize(transactions: list) -> list[NormalizationResult]:
    """Normalize a list of Transaction ORM objects. Returns results in same order."""
    return [
        normalize(t.raw_description, t.direction, t.amount)
        for t in transactions
    ]


def _clean_description(text: str) -> str:
    """
    Produce a human-readable normalized description by stripping
    leftover noise tokens after categorization.
    """
    # Remove POS prefix noise
    text = re.sub(r'^POS\s+\S+\s+', '', text)
    # Remove trailing reference fragments
    text = re.sub(r'\s+(REF|TXN|CR|DR)\s*$', '', text)
    return text.strip()
