"""
Unit tests for the financial scoring engine.

Tests cover:
  - DSCR calculation and label assignment
  - Revenue Volatility Index (coefficient of variation)
  - Revenue trend detection (first half vs second half)
  - Risk score composition and level bucketing
  - Burn rate and runway calculation
  - Monthly snapshot builder (category grouping)
  - Edge cases: empty input, zero revenue months, all-debit transactions
  - End-to-end score() with healthy, stressed, and anomalous profiles
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from decimal import Decimal
from datetime import date

from app.services.scorer import (
    score,
    _calc_dscr,
    _calc_rvi,
    _calc_trend,
    _calc_runway,
    _calc_risk_score,
    _build_monthly_snapshots,
    _dscr_label,
    _trend_label,
    _risk_level,
    MonthlySnapshot,
    ScoringResult,
)
from app.models.transaction import TransactionCategory, TransactionDirection
from app.models.report import RiskLevel


# ---------------------------------------------------------------------------
# Transaction builder helpers
# ---------------------------------------------------------------------------

D = TransactionDirection.DEBIT
C = TransactionDirection.CREDIT
CAT = TransactionCategory


def make_tx(
    year: int,
    month: int,
    amount: float,
    direction: TransactionDirection,
    category: TransactionCategory,
    day: int = 15,
) -> dict:
    return {
        "transaction_date": date(year, month, day),
        "amount": Decimal(str(amount)),
        "direction": direction,
        "category": category,
    }


def revenue_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, C, CAT.PRIMARY_REVENUE)


def payroll_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.PAYROLL)


def rent_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.COMMERCIAL_RENT)


def vat_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.GOVERNMENT_VAT)


def capex_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.DISCRETIONARY_CAPEX)


def charges_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.BANK_CHARGES)


def inter_tx(year: int, month: int, amount: float) -> dict:
    return make_tx(year, month, amount, D, CAT.INTER_ACCOUNT)


# Build a simple 6-month healthy business dataset
def healthy_6_months() -> list[dict]:
    txs = []
    for m in range(1, 7):
        txs.append(revenue_tx(2024, m, 10_000))
        txs.append(payroll_tx(2024, m, 3_000))
        txs.append(rent_tx(2024, m, 1_000))
        txs.append(vat_tx(2024, m, 500))
        txs.append(capex_tx(2024, m, 400))
        txs.append(charges_tx(2024, m, 100))
    return txs


# ---------------------------------------------------------------------------
# _calc_dscr
# ---------------------------------------------------------------------------

class TestCalcDscr:
    def test_strong_dscr(self):
        result = _calc_dscr(Decimal("10000"), Decimal("4000"))
        assert result == Decimal("2.5")

    def test_exactly_2x(self):
        result = _calc_dscr(Decimal("8000"), Decimal("4000"))
        assert result == Decimal("2")

    def test_borderline(self):
        result = _calc_dscr(Decimal("4100"), Decimal("4000"))
        assert result > Decimal("1") and result < Decimal("1.25")

    def test_insufficient(self):
        result = _calc_dscr(Decimal("3000"), Decimal("4000"))
        assert result < Decimal("1")

    def test_zero_obligations(self):
        result = _calc_dscr(Decimal("10000"), Decimal("0"))
        assert result == Decimal("0")

    def test_zero_revenue(self):
        result = _calc_dscr(Decimal("0"), Decimal("4000"))
        assert result == Decimal("0")


# ---------------------------------------------------------------------------
# _dscr_label
# ---------------------------------------------------------------------------

class TestDscrLabel:
    def test_strong(self):
        assert _dscr_label(Decimal("2.5")) == "STRONG"

    def test_exactly_strong_threshold(self):
        assert _dscr_label(Decimal("2.00")) == "STRONG"

    def test_good(self):
        assert _dscr_label(Decimal("1.75")) == "GOOD"

    def test_exactly_good_threshold(self):
        assert _dscr_label(Decimal("1.50")) == "GOOD"

    def test_acceptable(self):
        assert _dscr_label(Decimal("1.35")) == "ACCEPTABLE"

    def test_exactly_acceptable_threshold(self):
        assert _dscr_label(Decimal("1.25")) == "ACCEPTABLE"

    def test_borderline(self):
        assert _dscr_label(Decimal("1.10")) == "BORDERLINE"

    def test_exactly_borderline_threshold(self):
        assert _dscr_label(Decimal("1.00")) == "BORDERLINE"

    def test_insufficient(self):
        assert _dscr_label(Decimal("0.80")) == "INSUFFICIENT"

    def test_zero(self):
        assert _dscr_label(Decimal("0")) == "INSUFFICIENT"


# ---------------------------------------------------------------------------
# _calc_rvi
# ---------------------------------------------------------------------------

class TestCalcRvi:
    def test_zero_for_single_month(self):
        result = _calc_rvi([Decimal("10000")])
        assert result == Decimal("0")

    def test_zero_for_empty(self):
        result = _calc_rvi([])
        assert result == Decimal("0")

    def test_zero_for_zero_mean(self):
        result = _calc_rvi([Decimal("0"), Decimal("0")])
        assert result == Decimal("0")

    def test_stable_revenue(self):
        # Same revenue every month → std dev = 0 → RVI = 0
        revs = [Decimal("10000")] * 6
        result = _calc_rvi(revs)
        assert result == Decimal("0")

    def test_moderate_volatility(self):
        # Revenues that vary around 10k with ~20% cv
        revs = [Decimal(str(v)) for v in [8000, 9000, 10000, 11000, 12000, 10000]]
        result = _calc_rvi(revs)
        assert Decimal("0.10") < result < Decimal("0.25")

    def test_high_volatility(self):
        # Wild swings
        revs = [Decimal(str(v)) for v in [1000, 20000, 500, 18000, 2000, 15000]]
        result = _calc_rvi(revs)
        assert result > Decimal("0.50")

    def test_returns_decimal(self):
        revs = [Decimal("5000"), Decimal("7000")]
        result = _calc_rvi(revs)
        assert isinstance(result, Decimal)


# ---------------------------------------------------------------------------
# _calc_trend
# ---------------------------------------------------------------------------

class TestCalcTrend:
    def test_insufficient_data_returns_zero(self):
        snapshots = [
            MonthlySnapshot(2024, m, Decimal("10000"), Decimal("3000"),
                            Decimal("1000"), Decimal("500"), Decimal("400"),
                            Decimal("100"), Decimal("0"), Decimal("5000"))
            for m in range(1, 4)  # 3 months — below minimum
        ]
        assert _calc_trend(snapshots) == Decimal("0")

    def test_growing_trend(self):
        # First 3 months avg 5000, last 3 months avg 10000 → +100%
        revs = [5000, 5000, 5000, 10000, 10000, 10000]
        snapshots = [
            MonthlySnapshot(2024, m, Decimal(str(revs[m - 1])), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"))
            for m in range(1, 7)
        ]
        result = _calc_trend(snapshots)
        assert result == Decimal("100")

    def test_declining_trend(self):
        # First 3 months avg 10000, last 3 months avg 5000 → -50%
        revs = [10000, 10000, 10000, 5000, 5000, 5000]
        snapshots = [
            MonthlySnapshot(2024, m, Decimal(str(revs[m - 1])), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"))
            for m in range(1, 7)
        ]
        result = _calc_trend(snapshots)
        assert result == Decimal("-50")

    def test_stable_trend(self):
        revs = [10000] * 6
        snapshots = [
            MonthlySnapshot(2024, m, Decimal(str(revs[m - 1])), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"))
            for m in range(1, 7)
        ]
        result = _calc_trend(snapshots)
        assert result == Decimal("0")

    def test_zero_first_half_returns_zero(self):
        revs = [0, 0, 0, 5000, 5000, 5000]
        snapshots = [
            MonthlySnapshot(2024, m, Decimal(str(revs[m - 1])), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"),
                            Decimal("0"), Decimal("0"), Decimal("0"))
            for m in range(1, 7)
        ]
        result = _calc_trend(snapshots)
        assert result == Decimal("0")


# ---------------------------------------------------------------------------
# _trend_label
# ---------------------------------------------------------------------------

class TestTrendLabel:
    def test_growing(self):
        assert _trend_label(Decimal("10")) == "GROWING"

    def test_exactly_growing_threshold(self):
        assert _trend_label(Decimal("5")) == "GROWING"

    def test_stable_positive(self):
        assert _trend_label(Decimal("3")) == "STABLE"

    def test_stable_zero(self):
        assert _trend_label(Decimal("0")) == "STABLE"

    def test_stable_slight_negative(self):
        assert _trend_label(Decimal("-4")) == "STABLE"

    def test_exactly_stable_boundary(self):
        assert _trend_label(Decimal("-5")) == "STABLE"

    def test_declining(self):
        assert _trend_label(Decimal("-10")) == "DECLINING"


# ---------------------------------------------------------------------------
# _calc_runway
# ---------------------------------------------------------------------------

class TestCalcRunway:
    def test_no_runway_when_surplus(self):
        result = _calc_runway(Decimal("1000"), Decimal("8000"))
        assert result is None

    def test_no_runway_when_exactly_zero(self):
        result = _calc_runway(Decimal("0"), Decimal("8000"))
        assert result is None

    def test_runway_when_deficit(self):
        # Deficit of 1000/month, burn rate 8000/month → reserves = 24000 → 24 months
        result = _calc_runway(Decimal("-1000"), Decimal("8000"))
        assert result == Decimal("24")

    def test_runway_scales_with_deficit(self):
        # Deficit of 2000/month, burn 8000 → reserves = 24000 → 12 months
        result = _calc_runway(Decimal("-2000"), Decimal("8000"))
        assert result == Decimal("12")

    def test_high_burn_short_runway(self):
        # Deficit 4000/month, burn 4000 → reserves = 12000 → 3 months
        result = _calc_runway(Decimal("-4000"), Decimal("4000"))
        assert result == Decimal("3")


# ---------------------------------------------------------------------------
# _calc_risk_score
# ---------------------------------------------------------------------------

class TestCalcRiskScore:
    def test_best_case_low_risk(self):
        # DSCR=3.0 (0pts), RVI=0.05 (0pts), trend=+20% (0pts), surplus=+1000 (0pts)
        score = _calc_risk_score(Decimal("3.0"), Decimal("0.05"), Decimal("20"), Decimal("1000"))
        assert score == 0

    def test_worst_case_max_risk(self):
        # DSCR=0.5 (40pts), RVI=0.8 (25pts), trend=-20% (20pts), deficit (15pts)
        score = _calc_risk_score(Decimal("0.5"), Decimal("0.8"), Decimal("-20"), Decimal("-500"))
        assert score == 100

    def test_dscr_strong_zero_points(self):
        score = _calc_risk_score(Decimal("2.5"), Decimal("0.05"), Decimal("10"), Decimal("1000"))
        assert score == 0  # all components at 0

    def test_dscr_good_adds_10(self):
        score = _calc_risk_score(Decimal("1.7"), Decimal("0.05"), Decimal("10"), Decimal("1000"))
        assert score == 10  # only DSCR component adds points

    def test_dscr_acceptable_adds_20(self):
        score = _calc_risk_score(Decimal("1.30"), Decimal("0.05"), Decimal("10"), Decimal("1000"))
        assert score == 20

    def test_dscr_borderline_adds_30(self):
        score = _calc_risk_score(Decimal("1.05"), Decimal("0.05"), Decimal("10"), Decimal("1000"))
        assert score == 30

    def test_dscr_insufficient_adds_40(self):
        score = _calc_risk_score(Decimal("0.80"), Decimal("0.05"), Decimal("10"), Decimal("1000"))
        assert score == 40

    def test_burn_adds_15_when_deficit(self):
        score = _calc_risk_score(Decimal("2.5"), Decimal("0.05"), Decimal("10"), Decimal("-1"))
        assert score == 15

    def test_score_clamped_to_100(self):
        score = _calc_risk_score(Decimal("0"), Decimal("1.0"), Decimal("-50"), Decimal("-9999"))
        assert score <= 100

    def test_score_clamped_to_0(self):
        score = _calc_risk_score(Decimal("99"), Decimal("0"), Decimal("50"), Decimal("99999"))
        assert score >= 0


# ---------------------------------------------------------------------------
# _risk_level
# ---------------------------------------------------------------------------

class TestRiskLevel:
    def test_low(self):
        assert _risk_level(0) == RiskLevel.LOW
        assert _risk_level(20) == RiskLevel.LOW

    def test_medium(self):
        assert _risk_level(21) == RiskLevel.MEDIUM
        assert _risk_level(45) == RiskLevel.MEDIUM

    def test_high(self):
        assert _risk_level(46) == RiskLevel.HIGH
        assert _risk_level(70) == RiskLevel.HIGH

    def test_critical(self):
        assert _risk_level(71) == RiskLevel.CRITICAL
        assert _risk_level(100) == RiskLevel.CRITICAL


# ---------------------------------------------------------------------------
# _build_monthly_snapshots
# ---------------------------------------------------------------------------

class TestBuildMonthlySnapshots:
    def test_groups_by_month(self):
        txs = [
            revenue_tx(2024, 1, 10_000),
            revenue_tx(2024, 2, 12_000),
            payroll_tx(2024, 1, 3_000),
        ]
        snapshots = _build_monthly_snapshots(txs)
        assert len(snapshots) == 2
        assert snapshots[0].month == 1
        assert snapshots[0].revenue == Decimal("10000")
        assert snapshots[0].payroll == Decimal("3000")
        assert snapshots[1].month == 2
        assert snapshots[1].revenue == Decimal("12000")

    def test_sorted_chronologically(self):
        txs = [
            revenue_tx(2024, 6, 10_000),
            revenue_tx(2024, 1, 10_000),
            revenue_tx(2024, 3, 10_000),
        ]
        snapshots = _build_monthly_snapshots(txs)
        months = [s.month for s in snapshots]
        assert months == [1, 3, 6]

    def test_revenue_only_credits_counted(self):
        txs = [
            make_tx(2024, 1, 5000, C, CAT.PRIMARY_REVENUE),
            make_tx(2024, 1, 3000, D, CAT.PRIMARY_REVENUE),  # debit revenue — should not count
        ]
        snapshots = _build_monthly_snapshots(txs)
        assert snapshots[0].revenue == Decimal("5000")

    def test_total_debits_aggregated(self):
        txs = [
            payroll_tx(2024, 1, 3000),
            rent_tx(2024, 1, 1000),
            vat_tx(2024, 1, 500),
            capex_tx(2024, 1, 400),
            charges_tx(2024, 1, 100),
        ]
        snapshots = _build_monthly_snapshots(txs)
        assert snapshots[0].total_debits == Decimal("5000")

    def test_inter_account_in_total_debits(self):
        txs = [
            inter_tx(2024, 1, 2000),
        ]
        snapshots = _build_monthly_snapshots(txs)
        assert snapshots[0].inter_account_out == Decimal("2000")
        assert snapshots[0].total_debits == Decimal("2000")

    def test_empty_returns_empty(self):
        assert _build_monthly_snapshots([]) == []

    def test_cross_year_sorting(self):
        txs = [
            revenue_tx(2025, 1, 10_000),
            revenue_tx(2024, 12, 10_000),
        ]
        snapshots = _build_monthly_snapshots(txs)
        assert snapshots[0].year == 2024
        assert snapshots[1].year == 2025


# ---------------------------------------------------------------------------
# MonthlySnapshot properties
# ---------------------------------------------------------------------------

class TestMonthlySnapshotProperties:
    def _make(self, revenue=10000, payroll=3000, rent=1000, gov=500, capex=400, charges=100, inter=0, total_debits=5000):
        return MonthlySnapshot(
            year=2024, month=1,
            revenue=Decimal(str(revenue)),
            payroll=Decimal(str(payroll)),
            rent=Decimal(str(rent)),
            government_vat=Decimal(str(gov)),
            capex=Decimal(str(capex)),
            bank_charges=Decimal(str(charges)),
            inter_account_out=Decimal(str(inter)),
            total_debits=Decimal(str(total_debits)),
        )

    def test_label(self):
        snap = self._make()
        assert snap.label == "2024-01"

    def test_fixed_obligations(self):
        snap = self._make(payroll=3000, rent=1000, gov=500)
        assert snap.fixed_obligations == Decimal("4500")

    def test_total_operating_expenses(self):
        snap = self._make(payroll=3000, rent=1000, gov=500, capex=400, charges=100)
        assert snap.total_operating_expenses == Decimal("5000")

    def test_net_operating_income(self):
        snap = self._make(revenue=10000, payroll=3000, rent=1000, gov=500, capex=400, charges=100)
        assert snap.net_operating_income == Decimal("5000")

    def test_net_cash_flow(self):
        snap = self._make(revenue=10000, total_debits=7000)
        assert snap.net_cash_flow == Decimal("3000")

    def test_negative_net_when_expenses_exceed_revenue(self):
        snap = self._make(revenue=4000, payroll=3000, rent=1000, gov=500, capex=400, charges=100)
        assert snap.net_operating_income < 0


# ---------------------------------------------------------------------------
# Full score() integration
# ---------------------------------------------------------------------------

class TestScoreIntegration:
    def test_empty_transactions_returns_empty_result(self):
        result = score([])
        assert result.analysis_months == 0
        assert result.risk_score == 100
        assert result.risk_level == RiskLevel.CRITICAL
        assert result.dscr_label == "INSUFFICIENT_DATA"

    def test_healthy_business_low_risk(self):
        txs = healthy_6_months()
        result = score(txs)

        assert result.analysis_months == 6
        assert result.dscr > Decimal("2")
        assert result.dscr_label == "STRONG"
        assert result.risk_score <= 20
        assert result.risk_level == RiskLevel.LOW
        assert result.monthly_surplus_deficit > 0
        assert result.runway_months is None  # surplus → no runway needed

    def test_healthy_avg_revenue(self):
        txs = healthy_6_months()
        result = score(txs)
        assert result.avg_monthly_revenue == Decimal("10000.00")

    def test_healthy_total_revenue(self):
        txs = healthy_6_months()
        result = score(txs)
        assert result.total_revenue == Decimal("60000.00")

    def test_stressed_business_has_runway(self):
        txs = []
        for m in range(1, 7):
            txs.append(revenue_tx(2024, m, 4_000))   # low revenue
            txs.append(payroll_tx(2024, m, 3_000))
            txs.append(rent_tx(2024, m, 1_000))
            txs.append(vat_tx(2024, m, 500))
            txs.append(capex_tx(2024, m, 400))
            txs.append(charges_tx(2024, m, 100))
        result = score(txs)

        assert result.monthly_surplus_deficit < 0
        assert result.runway_months is not None
        assert result.risk_score > 30

    def test_zero_revenue_months_detected(self):
        txs = [
            revenue_tx(2024, 1, 10_000),
            payroll_tx(2024, 1, 3_000),
            # month 2 has only debit, no revenue
            payroll_tx(2024, 2, 3_000),
            revenue_tx(2024, 3, 10_000),
            payroll_tx(2024, 3, 3_000),
            revenue_tx(2024, 4, 10_000),
            payroll_tx(2024, 4, 3_000),
        ]
        result = score(txs)
        assert "2024-02" in result.zero_revenue_months

    def test_negative_net_months_detected(self):
        txs = [
            revenue_tx(2024, 1, 2_000),   # expenses will exceed revenue
            payroll_tx(2024, 1, 3_000),
            rent_tx(2024, 1, 1_000),
            revenue_tx(2024, 2, 10_000),
            payroll_tx(2024, 2, 3_000),
            revenue_tx(2024, 3, 10_000),
            payroll_tx(2024, 3, 3_000),
            revenue_tx(2024, 4, 10_000),
            payroll_tx(2024, 4, 3_000),
        ]
        result = score(txs)
        assert "2024-01" in result.negative_net_months

    def test_period_labels(self):
        txs = [
            revenue_tx(2024, 1, 10_000),
            revenue_tx(2024, 6, 10_000),
        ]
        result = score(txs)
        assert result.start_period == "2024-01"
        assert result.end_period == "2024-06"

    def test_growing_revenue_trend(self):
        txs = []
        for m in range(1, 4):
            txs.append(revenue_tx(2024, m, 5_000))
            txs.append(payroll_tx(2024, m, 2_000))
        for m in range(4, 7):
            txs.append(revenue_tx(2024, m, 15_000))
            txs.append(payroll_tx(2024, m, 2_000))
        result = score(txs)
        assert result.revenue_trend_label == "GROWING"
        assert result.revenue_trend_pct > Decimal("50")

    def test_declining_revenue_trend(self):
        txs = []
        for m in range(1, 4):
            txs.append(revenue_tx(2024, m, 15_000))
            txs.append(payroll_tx(2024, m, 2_000))
        for m in range(4, 7):
            txs.append(revenue_tx(2024, m, 5_000))
            txs.append(payroll_tx(2024, m, 2_000))
        result = score(txs)
        assert result.revenue_trend_label == "DECLINING"
        assert result.revenue_trend_pct < Decimal("-50")

    def test_result_fields_are_decimal(self):
        txs = healthy_6_months()
        result = score(txs)
        assert isinstance(result.avg_monthly_revenue, Decimal)
        assert isinstance(result.dscr, Decimal)
        assert isinstance(result.revenue_volatility_index, Decimal)
        assert isinstance(result.burn_rate_monthly, Decimal)

    def test_risk_score_is_int(self):
        result = score(healthy_6_months())
        assert isinstance(result.risk_score, int)

    def test_stable_revenue_zero_rvi(self):
        txs = healthy_6_months()  # all months have identical revenue
        result = score(txs)
        assert result.revenue_volatility_index == Decimal("0.0000")

    def test_only_debit_transactions(self):
        txs = [payroll_tx(2024, m, 3000) for m in range(1, 7)]
        result = score(txs)
        assert result.avg_monthly_revenue == Decimal("0.00")
        assert result.dscr_label in ("INSUFFICIENT", "INSUFFICIENT_DATA")

    def test_inter_account_not_in_operating_expenses(self):
        # Inter-account transfers inflate total_debits but not operating expenses
        txs = [
            revenue_tx(2024, 1, 10_000),
            payroll_tx(2024, 1, 3_000),
            inter_tx(2024, 1, 5_000),   # large internal transfer
        ]
        result = score(txs)
        # Burn rate includes inter-account (total debits)
        assert result.burn_rate_monthly >= Decimal("8000")
        # But monthly surplus uses avg_expenses (operating only), not total debits
        # avg expenses = payroll only (3000), surplus = 10000 - 3000 = 7000
        assert result.monthly_surplus_deficit == Decimal("7000.00")

    def test_single_month_returns_result(self):
        txs = [
            revenue_tx(2024, 1, 10_000),
            payroll_tx(2024, 1, 3_000),
        ]
        result = score(txs)
        assert result.analysis_months == 1
        assert result.start_period == result.end_period == "2024-01"
