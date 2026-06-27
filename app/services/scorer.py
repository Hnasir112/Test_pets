"""
Financial Scoring Engine.

Takes categorized transaction records and computes the core credit
metrics used by GCC commercial banks to assess SME creditworthiness.

All calculations are pure functions — no database access, no side effects.
Input: list of transaction dicts with date, amount, direction, category.
Output: ScoringResult dataclass.

Core metrics:
  DSCR                  — Debt Service Coverage Ratio (primary credit signal)
  Revenue Volatility    — Coefficient of variation on monthly revenue
  Burn Rate & Runway    — Monthly cash consumption and depletion timeline
  Revenue Trend         — Linear regression slope over the analysis window
  Risk Score (0–100)    — Composite weighted risk signal for the credit report
"""

import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from app.models.transaction import TransactionCategory, TransactionDirection
from app.models.report import RiskLevel


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MonthlySnapshot:
    year: int
    month: int
    revenue: Decimal
    payroll: Decimal
    rent: Decimal
    government_vat: Decimal
    capex: Decimal
    bank_charges: Decimal
    inter_account_out: Decimal
    total_debits: Decimal

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    @property
    def fixed_obligations(self) -> Decimal:
        """Payroll + Rent + Govt VAT — the non-discretionary monthly commitments."""
        return self.payroll + self.rent + self.government_vat

    @property
    def total_operating_expenses(self) -> Decimal:
        return self.payroll + self.rent + self.government_vat + self.capex + self.bank_charges

    @property
    def net_operating_income(self) -> Decimal:
        """Revenue minus all operating expenses."""
        return self.revenue - self.total_operating_expenses

    @property
    def net_cash_flow(self) -> Decimal:
        """Revenue minus ALL outflows including inter-account."""
        return self.revenue - self.total_debits


@dataclass
class ScoringResult:
    # Period
    analysis_months: int
    start_period: str
    end_period: str

    # Monthly detail
    monthly_snapshots: list[MonthlySnapshot]

    # Revenue metrics
    avg_monthly_revenue: Decimal
    total_revenue: Decimal
    revenue_volatility_index: Decimal   # coefficient of variation (std/mean)
    revenue_trend_pct: Decimal          # % change first half vs second half
    revenue_trend_label: str            # GROWING / STABLE / DECLINING

    # Expense metrics
    avg_monthly_payroll: Decimal
    avg_monthly_rent: Decimal
    avg_monthly_government_vat: Decimal
    avg_monthly_capex: Decimal
    avg_monthly_bank_charges: Decimal
    avg_monthly_total_expenses: Decimal

    # Net metrics
    avg_monthly_net: Decimal
    avg_monthly_fixed_obligations: Decimal

    # DSCR
    dscr: Decimal
    dscr_label: str                     # STRONG / GOOD / ACCEPTABLE / BORDERLINE / INSUFFICIENT

    # Burn rate & runway
    burn_rate_monthly: Decimal          # average monthly total outflows
    monthly_surplus_deficit: Decimal    # avg_revenue - avg_total_expenses (+ = surplus)
    runway_months: Optional[Decimal]    # None if surplus (not burning)

    # Risk
    risk_score: int                     # 0–100 (higher = riskier)
    risk_level: RiskLevel

    # Anomaly hints for the detector
    zero_revenue_months: list[str]      # months with no revenue recorded
    negative_net_months: list[str]      # months where expenses > revenue


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEBIT = TransactionDirection.DEBIT
CREDIT = TransactionDirection.CREDIT

CAT = TransactionCategory

# DSCR thresholds (NBB/BBK standard for SME commercial lending)
DSCR_STRONG      = Decimal("2.00")
DSCR_GOOD        = Decimal("1.50")
DSCR_ACCEPTABLE  = Decimal("1.25")
DSCR_BORDERLINE  = Decimal("1.00")

# Revenue volatility index bands
RVI_STABLE       = Decimal("0.15")
RVI_MODERATE     = Decimal("0.30")
RVI_HIGH         = Decimal("0.50")

# Risk score weights (must sum to 1.0)
W_DSCR        = 0.40
W_VOLATILITY  = 0.25
W_TREND       = 0.20
W_BURN        = 0.15

_TWO = Decimal("0.01")   # quantize target for 2dp
_FOUR = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def score(transactions: list[dict]) -> ScoringResult:
    """
    Compute full credit scorecard from a list of categorized transactions.

    Args:
        transactions: list of dicts with keys:
            transaction_date (date | datetime)
            amount (Decimal)
            direction (TransactionDirection)
            category (TransactionCategory)

    Returns:
        ScoringResult with all computed metrics.
    """
    if not transactions:
        return _empty_result()

    monthly = _build_monthly_snapshots(transactions)

    if not monthly:
        return _empty_result()

    avg_revenue   = _avg([m.revenue for m in monthly])
    avg_payroll   = _avg([m.payroll for m in monthly])
    avg_rent      = _avg([m.rent for m in monthly])
    avg_govt      = _avg([m.government_vat for m in monthly])
    avg_capex     = _avg([m.capex for m in monthly])
    avg_charges   = _avg([m.bank_charges for m in monthly])
    avg_expenses  = _avg([m.total_operating_expenses for m in monthly])
    avg_fixed     = _avg([m.fixed_obligations for m in monthly])
    avg_net       = _avg([m.net_operating_income for m in monthly])
    total_revenue = sum((m.revenue for m in monthly), Decimal("0"))
    burn_rate     = _avg([m.total_debits for m in monthly])
    surplus       = avg_revenue - avg_expenses

    dscr          = _calc_dscr(avg_revenue, avg_fixed)
    rvi           = _calc_rvi([m.revenue for m in monthly])
    trend_pct     = _calc_trend(monthly)
    trend_label   = _trend_label(trend_pct)
    runway        = _calc_runway(surplus, burn_rate)
    risk_score    = _calc_risk_score(dscr, rvi, trend_pct, surplus)
    risk_level    = _risk_level(risk_score)

    zero_rev_months = [m.label for m in monthly if m.revenue == 0]
    neg_net_months  = [m.label for m in monthly if m.net_operating_income < 0]

    return ScoringResult(
        analysis_months=len(monthly),
        start_period=monthly[0].label,
        end_period=monthly[-1].label,
        monthly_snapshots=monthly,
        avg_monthly_revenue=avg_revenue.quantize(_TWO, ROUND_HALF_UP),
        total_revenue=total_revenue.quantize(_TWO, ROUND_HALF_UP),
        revenue_volatility_index=rvi.quantize(_FOUR, ROUND_HALF_UP),
        revenue_trend_pct=trend_pct.quantize(_TWO, ROUND_HALF_UP),
        revenue_trend_label=trend_label,
        avg_monthly_payroll=avg_payroll.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_rent=avg_rent.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_government_vat=avg_govt.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_capex=avg_capex.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_bank_charges=avg_charges.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_total_expenses=avg_expenses.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_net=avg_net.quantize(_TWO, ROUND_HALF_UP),
        avg_monthly_fixed_obligations=avg_fixed.quantize(_TWO, ROUND_HALF_UP),
        dscr=dscr.quantize(_FOUR, ROUND_HALF_UP),
        dscr_label=_dscr_label(dscr),
        burn_rate_monthly=burn_rate.quantize(_TWO, ROUND_HALF_UP),
        monthly_surplus_deficit=surplus.quantize(_TWO, ROUND_HALF_UP),
        runway_months=runway.quantize(_TWO, ROUND_HALF_UP) if runway is not None else None,
        risk_score=risk_score,
        risk_level=risk_level,
        zero_revenue_months=zero_rev_months,
        negative_net_months=neg_net_months,
    )


# ---------------------------------------------------------------------------
# Monthly snapshot builder
# ---------------------------------------------------------------------------

def _build_monthly_snapshots(transactions: list[dict]) -> list[MonthlySnapshot]:
    buckets: dict[tuple[int, int], dict[str, Decimal]] = {}

    def _zero_bucket():
        return {
            "revenue": Decimal("0"),
            "payroll": Decimal("0"),
            "rent": Decimal("0"),
            "government_vat": Decimal("0"),
            "capex": Decimal("0"),
            "bank_charges": Decimal("0"),
            "inter_account_out": Decimal("0"),
            "total_debits": Decimal("0"),
        }

    for t in transactions:
        dt = t["transaction_date"]
        if hasattr(dt, "year"):
            key = (dt.year, dt.month)
        else:
            key = (dt.year, dt.month)

        if key not in buckets:
            buckets[key] = _zero_bucket()

        amount = Decimal(str(t["amount"]))
        direction = t["direction"]
        category = t["category"]

        if direction == CREDIT and category == CAT.PRIMARY_REVENUE:
            buckets[key]["revenue"] += amount

        if direction == DEBIT:
            buckets[key]["total_debits"] += amount

            if category == CAT.PAYROLL:
                buckets[key]["payroll"] += amount
            elif category == CAT.COMMERCIAL_RENT:
                buckets[key]["rent"] += amount
            elif category == CAT.GOVERNMENT_VAT:
                buckets[key]["government_vat"] += amount
            elif category == CAT.DISCRETIONARY_CAPEX:
                buckets[key]["capex"] += amount
            elif category == CAT.BANK_CHARGES:
                buckets[key]["bank_charges"] += amount
            elif category == CAT.INTER_ACCOUNT:
                buckets[key]["inter_account_out"] += amount

    snapshots = []
    for (year, month), b in sorted(buckets.items()):
        snapshots.append(MonthlySnapshot(
            year=year, month=month,
            revenue=b["revenue"],
            payroll=b["payroll"],
            rent=b["rent"],
            government_vat=b["government_vat"],
            capex=b["capex"],
            bank_charges=b["bank_charges"],
            inter_account_out=b["inter_account_out"],
            total_debits=b["total_debits"],
        ))
    return snapshots


# ---------------------------------------------------------------------------
# Individual metric calculations
# ---------------------------------------------------------------------------

def _calc_dscr(avg_revenue: Decimal, avg_fixed_obligations: Decimal) -> Decimal:
    """
    DSCR = Average Monthly Revenue / Average Monthly Fixed Obligations
    (Payroll + Rent + Government VAT)

    Fixed obligations are used as the denominator because they represent
    the non-discretionary monthly commitments a business cannot defer —
    the closest proxy to "debt service" visible in transaction data.

    Returns 0 if denominator is zero (no obligations recorded).
    """
    if avg_fixed_obligations <= 0:
        return Decimal("0")
    return avg_revenue / avg_fixed_obligations


def _calc_rvi(monthly_revenues: list[Decimal]) -> Decimal:
    """
    Revenue Volatility Index = Standard Deviation / Mean of monthly revenues.
    Also known as Coefficient of Variation. Higher = more volatile.

    Returns 0 if fewer than 2 months or zero mean.
    """
    if len(monthly_revenues) < 2:
        return Decimal("0")
    floats = [float(r) for r in monthly_revenues]
    mean = statistics.mean(floats)
    if mean == 0:
        return Decimal("0")
    return Decimal(str(statistics.stdev(floats) / mean))


def _calc_trend(monthly: list[MonthlySnapshot]) -> Decimal:
    """
    Revenue trend: compare average revenue in first half vs second half
    of the analysis window. Returns % change (positive = growing).

    For < 4 months of data, returns 0 (insufficient for trend).
    """
    if len(monthly) < 4:
        return Decimal("0")

    mid = len(monthly) // 2
    first_half_avg  = _avg([m.revenue for m in monthly[:mid]])
    second_half_avg = _avg([m.revenue for m in monthly[mid:]])

    if first_half_avg == 0:
        return Decimal("0")

    pct_change = ((second_half_avg - first_half_avg) / first_half_avg) * 100
    return pct_change


def _calc_runway(monthly_surplus: Decimal, burn_rate: Decimal) -> Optional[Decimal]:
    """
    Runway in months — only meaningful when the business is burning cash
    (monthly surplus is negative).

    Returns None when surplus >= 0 (business is cash-flow positive).
    Assumes 3 months of operating reserves when calculating depletion.
    """
    if monthly_surplus >= 0:
        return None
    estimated_reserves = burn_rate * 3
    monthly_deficit = abs(monthly_surplus)
    if monthly_deficit == 0:
        return None
    return estimated_reserves / monthly_deficit


def _calc_risk_score(
    dscr: Decimal,
    rvi: Decimal,
    trend_pct: Decimal,
    surplus: Decimal,
) -> int:
    """
    Composite risk score 0–100. Higher = riskier.

    Four weighted components:
      DSCR (40%)        — core affordability signal
      Volatility (25%)  — revenue predictability
      Trend (20%)       — business trajectory
      Burn rate (15%)   — cash sustainability
    """
    # DSCR component (0–40 points)
    d = float(dscr)
    if d >= 2.0:
        dscr_pts = 0
    elif d >= 1.5:
        dscr_pts = 10
    elif d >= 1.25:
        dscr_pts = 20
    elif d >= 1.0:
        dscr_pts = 30
    else:
        dscr_pts = 40

    # Volatility component (0–25 points)
    v = float(rvi)
    if v < 0.15:
        vol_pts = 0
    elif v < 0.30:
        vol_pts = 8
    elif v < 0.50:
        vol_pts = 17
    else:
        vol_pts = 25

    # Trend component (0–20 points)
    t = float(trend_pct)
    if t >= 5:
        trend_pts = 0
    elif t >= 0:
        trend_pts = 6
    elif t >= -10:
        trend_pts = 13
    else:
        trend_pts = 20

    # Burn component (0–15 points)
    burn_pts = 0 if surplus >= 0 else 15

    total = dscr_pts + vol_pts + trend_pts + burn_pts
    return min(100, max(0, total))


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def _dscr_label(dscr: Decimal) -> str:
    if dscr >= DSCR_STRONG:
        return "STRONG"
    if dscr >= DSCR_GOOD:
        return "GOOD"
    if dscr >= DSCR_ACCEPTABLE:
        return "ACCEPTABLE"
    if dscr >= DSCR_BORDERLINE:
        return "BORDERLINE"
    return "INSUFFICIENT"


def _trend_label(trend_pct: Decimal) -> str:
    if trend_pct >= 5:
        return "GROWING"
    if trend_pct >= -5:
        return "STABLE"
    return "DECLINING"


def _risk_level(score: int) -> RiskLevel:
    if score <= 20:
        return RiskLevel.LOW
    if score <= 45:
        return RiskLevel.MEDIUM
    if score <= 70:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _avg(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(str(len(values)))


def _empty_result() -> ScoringResult:
    return ScoringResult(
        analysis_months=0,
        start_period="N/A",
        end_period="N/A",
        monthly_snapshots=[],
        avg_monthly_revenue=Decimal("0"),
        total_revenue=Decimal("0"),
        revenue_volatility_index=Decimal("0"),
        revenue_trend_pct=Decimal("0"),
        revenue_trend_label="INSUFFICIENT_DATA",
        avg_monthly_payroll=Decimal("0"),
        avg_monthly_rent=Decimal("0"),
        avg_monthly_government_vat=Decimal("0"),
        avg_monthly_capex=Decimal("0"),
        avg_monthly_bank_charges=Decimal("0"),
        avg_monthly_total_expenses=Decimal("0"),
        avg_monthly_net=Decimal("0"),
        avg_monthly_fixed_obligations=Decimal("0"),
        dscr=Decimal("0"),
        dscr_label="INSUFFICIENT_DATA",
        burn_rate_monthly=Decimal("0"),
        monthly_surplus_deficit=Decimal("0"),
        runway_months=None,
        risk_score=100,
        risk_level=RiskLevel.CRITICAL,
        zero_revenue_months=[],
        negative_net_months=[],
    )
