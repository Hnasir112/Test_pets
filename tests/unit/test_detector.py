"""
Unit tests for the anomaly detection engine.

Tests cover:
  - Round-trip revenue detection (WARNING and CRITICAL thresholds)
  - Zero revenue months with active outflows
  - Revenue spike detection (3× and 5× baselines)
  - Payroll irregularity (missing WPS in active months)
  - High inter-account share of total outflows
  - Revenue concentration (single-counterparty dominance)
  - Sustained deficit (3+ consecutive negative-net months)
  - Output sorting (CRITICAL before WARNING before INFO)
  - Clean accounts produce no flags
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from decimal import Decimal
from datetime import date

from app.services.detector import detect, DetectedFlag, _check_round_trip, _check_zero_revenue, _check_revenue_spike, _check_payroll_irregularity, _check_high_inter_account, _check_revenue_concentration, _check_sustained_deficit
from app.services.scorer import score, MonthlySnapshot, ScoringResult
from app.models.transaction import TransactionCategory, TransactionDirection
from app.models.report import FlagSeverity, RiskLevel


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

D = TransactionDirection.DEBIT
C = TransactionDirection.CREDIT
CAT = TransactionCategory


def make_tx(year, month, amount, direction, category, day=15):
    return {
        "transaction_date": date(year, month, day),
        "amount": Decimal(str(amount)),
        "direction": direction,
        "category": category,
    }


def snap(year, month, revenue=10000, payroll=3000, rent=1000, gov=500,
         capex=400, charges=100, inter=0, total_debits=None):
    if total_debits is None:
        total_debits = payroll + rent + gov + capex + charges + inter
    return MonthlySnapshot(
        year=year, month=month,
        revenue=Decimal(str(revenue)),
        payroll=Decimal(str(payroll)),
        rent=Decimal(str(rent)),
        government_vat=Decimal(str(gov)),
        capex=Decimal(str(capex)),
        bank_charges=Decimal(str(charges)),
        inter_account_out=Decimal(str(inter)),
        total_debits=Decimal(str(total_debits)),
    )


def make_result(snapshots: list[MonthlySnapshot]) -> ScoringResult:
    """Minimal ScoringResult wrapper around pre-built snapshots."""
    from app.services.scorer import _avg
    monthly = snapshots
    avg_revenue = _avg([m.revenue for m in monthly])
    avg_fixed = _avg([m.payroll + m.rent + m.government_vat for m in monthly])
    dscr = avg_revenue / avg_fixed if avg_fixed > 0 else Decimal("0")
    return ScoringResult(
        analysis_months=len(monthly),
        start_period=monthly[0].label if monthly else "N/A",
        end_period=monthly[-1].label if monthly else "N/A",
        monthly_snapshots=monthly,
        avg_monthly_revenue=avg_revenue,
        total_revenue=sum((m.revenue for m in monthly), Decimal("0")),
        revenue_volatility_index=Decimal("0"),
        revenue_trend_pct=Decimal("0"),
        revenue_trend_label="STABLE",
        avg_monthly_payroll=_avg([m.payroll for m in monthly]),
        avg_monthly_rent=_avg([m.rent for m in monthly]),
        avg_monthly_government_vat=_avg([m.government_vat for m in monthly]),
        avg_monthly_capex=_avg([m.capex for m in monthly]),
        avg_monthly_bank_charges=_avg([m.bank_charges for m in monthly]),
        avg_monthly_total_expenses=_avg([m.total_operating_expenses for m in monthly]),
        avg_monthly_net=_avg([m.net_operating_income for m in monthly]),
        avg_monthly_fixed_obligations=avg_fixed,
        dscr=dscr,
        dscr_label="STRONG",
        burn_rate_monthly=_avg([m.total_debits for m in monthly]),
        monthly_surplus_deficit=Decimal("0"),
        runway_months=None,
        risk_score=10,
        risk_level=RiskLevel.LOW,
        zero_revenue_months=[],
        negative_net_months=[],
    )


def healthy_txs():
    txs = []
    for m in range(1, 7):
        txs.append(make_tx(2024, m, 10_000, C, CAT.PRIMARY_REVENUE))
        txs.append(make_tx(2024, m, 3_000, D, CAT.PAYROLL))
        txs.append(make_tx(2024, m, 1_000, D, CAT.COMMERCIAL_RENT))
        txs.append(make_tx(2024, m, 500,   D, CAT.GOVERNMENT_VAT))
        txs.append(make_tx(2024, m, 400,   D, CAT.DISCRETIONARY_CAPEX))
        txs.append(make_tx(2024, m, 100,   D, CAT.BANK_CHARGES))
    return txs


# ---------------------------------------------------------------------------
# _check_round_trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_critical_when_inter_account_gte_70_pct(self):
        snapshots = [snap(2024, 1, revenue=10000, inter=7500, total_debits=7500)]
        flags = _check_round_trip(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.CRITICAL
        assert flags[0].flag_type == "ROUND_TRIP_REVENUE"

    def test_warning_when_inter_account_40_to_70_pct(self):
        snapshots = [snap(2024, 1, revenue=10000, inter=5000, total_debits=5000)]
        flags = _check_round_trip(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.WARNING

    def test_no_flag_below_40_pct(self):
        snapshots = [snap(2024, 1, revenue=10000, inter=2000, total_debits=5000)]
        flags = _check_round_trip(snapshots)
        assert flags == []

    def test_no_flag_when_revenue_is_zero(self):
        snapshots = [snap(2024, 1, revenue=0, inter=5000, total_debits=5000)]
        flags = _check_round_trip(snapshots)
        assert flags == []

    def test_month_label_populated(self):
        snapshots = [snap(2024, 3, revenue=10000, inter=8000, total_debits=8000)]
        flags = _check_round_trip(snapshots)
        assert flags[0].month == "2024-03"

    def test_multiple_months_flagged_independently(self):
        snapshots = [
            snap(2024, 1, revenue=10000, inter=8000, total_debits=8000),  # CRITICAL
            snap(2024, 2, revenue=10000, inter=4500, total_debits=4500),  # WARNING
            snap(2024, 3, revenue=10000, inter=1000, total_debits=5000),  # clean
        ]
        flags = _check_round_trip(snapshots)
        assert len(flags) == 2
        severities = {f.severity for f in flags}
        assert FlagSeverity.CRITICAL in severities
        assert FlagSeverity.WARNING in severities

    def test_exactly_70_pct_is_critical(self):
        snapshots = [snap(2024, 1, revenue=10000, inter=7000, total_debits=7000)]
        flags = _check_round_trip(snapshots)
        assert flags[0].severity == FlagSeverity.CRITICAL

    def test_exactly_40_pct_is_warning(self):
        snapshots = [snap(2024, 1, revenue=10000, inter=4000, total_debits=4000)]
        flags = _check_round_trip(snapshots)
        assert flags[0].severity == FlagSeverity.WARNING


# ---------------------------------------------------------------------------
# _check_zero_revenue
# ---------------------------------------------------------------------------

class TestZeroRevenue:
    def test_critical_when_large_outflows_and_no_revenue(self):
        snapshots = [snap(2024, 1, revenue=0, payroll=3000, total_debits=5000)]
        flags = _check_zero_revenue(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.CRITICAL
        assert flags[0].flag_type == "ZERO_REVENUE_MONTH"

    def test_warning_when_small_outflows_and_no_revenue(self):
        snapshots = [snap(2024, 1, revenue=0, payroll=0, capex=0, charges=0,
                          rent=0, gov=0, inter=0, total_debits=500)]
        flags = _check_zero_revenue(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.WARNING

    def test_no_flag_when_fully_inactive_month(self):
        # Zero revenue AND zero debits — could be early/partial month
        snapshots = [snap(2024, 1, revenue=0, total_debits=0, payroll=0,
                          rent=0, gov=0, capex=0, charges=0, inter=0)]
        flags = _check_zero_revenue(snapshots)
        assert flags == []

    def test_no_flag_when_revenue_positive(self):
        snapshots = [snap(2024, 1, revenue=5000)]
        flags = _check_zero_revenue(snapshots)
        assert flags == []


# ---------------------------------------------------------------------------
# _check_revenue_spike
# ---------------------------------------------------------------------------

class TestRevenueSpike:
    def _make_snapshots(self, revenues: list[float]) -> list[MonthlySnapshot]:
        return [
            snap(2024, m + 1, revenue=r)
            for m, r in enumerate(revenues)
        ]

    def test_critical_spike_5x(self):
        snapshots = self._make_snapshots([10000, 10000, 10000, 10000, 60000, 10000])
        flags = _check_revenue_spike(snapshots)
        spike_flags = [f for f in flags if f.flag_type == "REVENUE_SPIKE"]
        assert any(f.severity == FlagSeverity.CRITICAL for f in spike_flags)

    def test_warning_spike_3x(self):
        snapshots = self._make_snapshots([10000, 10000, 10000, 10000, 35000, 10000])
        flags = _check_revenue_spike(snapshots)
        spike_flags = [f for f in flags if f.flag_type == "REVENUE_SPIKE"]
        assert any(f.severity == FlagSeverity.WARNING for f in spike_flags)

    def test_no_flag_for_consistent_revenue(self):
        snapshots = self._make_snapshots([10000, 10000, 10000, 10000, 10000, 10000])
        flags = _check_revenue_spike(snapshots)
        assert flags == []

    def test_no_flag_for_modest_variation(self):
        snapshots = self._make_snapshots([8000, 9000, 10000, 11000, 12000, 10000])
        flags = _check_revenue_spike(snapshots)
        assert flags == []

    def test_insufficient_data_returns_empty(self):
        snapshots = self._make_snapshots([10000, 60000])  # only 2 months
        flags = _check_revenue_spike(snapshots)
        assert flags == []

    def test_zero_revenue_month_not_spiked(self):
        # Month with zero revenue should not be flagged as a spike
        snapshots = self._make_snapshots([10000, 0, 10000, 10000, 10000, 10000])
        flags = _check_revenue_spike(snapshots)
        zero_spike = [f for f in flags if f.month and "02" in f.month and f.flag_type == "REVENUE_SPIKE"]
        assert zero_spike == []


# ---------------------------------------------------------------------------
# _check_payroll_irregularity
# ---------------------------------------------------------------------------

class TestPayrollIrregularity:
    def test_flags_missing_payroll_month(self):
        snapshots = [
            snap(2024, 1, payroll=3000, total_debits=5000),
            snap(2024, 2, payroll=0, total_debits=5000),   # missing payroll
            snap(2024, 3, payroll=3000, total_debits=5000),
        ]
        flags = _check_payroll_irregularity(snapshots)
        assert len(flags) == 1
        assert flags[0].flag_type == "PAYROLL_IRREGULARITY"
        assert flags[0].month == "2024-02"

    def test_no_flag_when_avg_payroll_is_zero(self):
        # Business never pays via WPS — not a salaried business
        snapshots = [
            snap(2024, m, payroll=0, total_debits=5000)
            for m in range(1, 4)
        ]
        flags = _check_payroll_irregularity(snapshots)
        assert flags == []

    def test_critical_when_large_outflows_but_no_payroll(self):
        snapshots = [
            snap(2024, 1, payroll=3000, total_debits=5000),
            snap(2024, 2, payroll=0, total_debits=8000),   # debits > avg payroll
            snap(2024, 3, payroll=3000, total_debits=5000),
        ]
        flags = _check_payroll_irregularity(snapshots)
        assert flags[0].severity == FlagSeverity.CRITICAL

    def test_warning_when_small_outflows_but_no_payroll(self):
        snapshots = [
            snap(2024, 1, payroll=5000, total_debits=7000),
            snap(2024, 2, payroll=0, total_debits=2000),   # debits < avg payroll
            snap(2024, 3, payroll=5000, total_debits=7000),
        ]
        flags = _check_payroll_irregularity(snapshots)
        assert flags[0].severity == FlagSeverity.WARNING

    def test_no_flag_when_payroll_present_every_month(self):
        snapshots = [snap(2024, m, payroll=3000) for m in range(1, 7)]
        flags = _check_payroll_irregularity(snapshots)
        assert flags == []

    def test_no_flag_when_month_is_fully_inactive(self):
        snapshots = [
            snap(2024, 1, payroll=3000, total_debits=5000),
            snap(2024, 2, payroll=0, total_debits=0),   # inactive month
            snap(2024, 3, payroll=3000, total_debits=5000),
        ]
        flags = _check_payroll_irregularity(snapshots)
        assert flags == []


# ---------------------------------------------------------------------------
# _check_high_inter_account
# ---------------------------------------------------------------------------

class TestHighInterAccount:
    def test_critical_when_ia_over_50_pct(self):
        snapshots = [snap(2024, 1, inter=6000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.CRITICAL
        assert flags[0].flag_type == "HIGH_INTER_ACCOUNT"

    def test_warning_when_ia_30_to_50_pct(self):
        snapshots = [snap(2024, 1, inter=4000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert len(flags) == 1
        assert flags[0].severity == FlagSeverity.WARNING

    def test_no_flag_below_30_pct(self):
        snapshots = [snap(2024, 1, inter=2000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert flags == []

    def test_no_flag_when_no_debits(self):
        snapshots = [snap(2024, 1, inter=0, total_debits=0)]
        flags = _check_high_inter_account(snapshots)
        assert flags == []

    def test_aggregates_across_months(self):
        # 2000 IA over 4000 total debits each month → 50% → CRITICAL
        snapshots = [snap(2024, m, inter=2000, total_debits=4000) for m in range(1, 4)]
        flags = _check_high_inter_account(snapshots)
        assert flags[0].severity == FlagSeverity.CRITICAL

    def test_exactly_50_pct_is_critical(self):
        snapshots = [snap(2024, 1, inter=5000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert flags[0].severity == FlagSeverity.CRITICAL

    def test_exactly_30_pct_is_warning(self):
        snapshots = [snap(2024, 1, inter=3000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert flags[0].severity == FlagSeverity.WARNING

    def test_portfolio_level_flag_has_no_month(self):
        snapshots = [snap(2024, 1, inter=6000, total_debits=10000)]
        flags = _check_high_inter_account(snapshots)
        assert flags[0].month is None


# ---------------------------------------------------------------------------
# _check_revenue_concentration
# ---------------------------------------------------------------------------

class TestRevenueConcentration:
    def _txs(self, year, month, amounts: list[float]):
        return [
            make_tx(year, month, a, C, CAT.PRIMARY_REVENUE)
            for a in amounts
        ]

    def test_flags_dominant_single_receipt(self):
        txs = self._txs(2024, 1, [9000, 500, 500])  # 9000 = 90% of 10000
        flags = _check_revenue_concentration(txs)
        assert len(flags) == 1
        assert flags[0].flag_type == "REVENUE_CONCENTRATION"
        assert flags[0].severity == FlagSeverity.INFO

    def test_no_flag_when_only_one_receipt(self):
        txs = self._txs(2024, 1, [10000])  # single receipt — no concentration within month
        flags = _check_revenue_concentration(txs)
        assert flags == []

    def test_no_flag_when_evenly_spread(self):
        txs = self._txs(2024, 1, [3333, 3333, 3334])
        flags = _check_revenue_concentration(txs)
        assert flags == []

    def test_no_flag_for_debits(self):
        txs = [make_tx(2024, 1, 9000, D, CAT.PAYROLL),
               make_tx(2024, 1, 500, D, CAT.PAYROLL)]
        flags = _check_revenue_concentration(txs)
        assert flags == []

    def test_no_flag_for_non_revenue_credits(self):
        txs = [make_tx(2024, 1, 9000, C, CAT.INTER_ACCOUNT),
               make_tx(2024, 1, 500, C, CAT.INTER_ACCOUNT)]
        flags = _check_revenue_concentration(txs)
        assert flags == []

    def test_month_label_in_flag(self):
        txs = self._txs(2024, 5, [9000, 1000])
        flags = _check_revenue_concentration(txs)
        assert flags[0].month == "2024-05"

    def test_exactly_80_pct_triggers_flag(self):
        txs = self._txs(2024, 1, [8000, 2000])
        flags = _check_revenue_concentration(txs)
        assert len(flags) == 1


# ---------------------------------------------------------------------------
# _check_sustained_deficit
# ---------------------------------------------------------------------------

class TestSustainedDeficit:
    def _snap_net(self, year, month, net):
        # Build a snapshot where net_operating_income equals `net`
        # net = revenue - (payroll + rent + gov + capex + charges)
        revenue = max(net + 5000, 0)
        expenses = revenue - net
        return snap(year, month, revenue=revenue, payroll=expenses,
                    rent=0, gov=0, capex=0, charges=0, total_debits=expenses)

    def test_three_consecutive_negative_months_warning(self):
        snapshots = [self._snap_net(2024, m, -500) for m in range(1, 4)]
        flags = _check_sustained_deficit(snapshots)
        assert len(flags) == 1
        assert flags[0].flag_type == "SUSTAINED_DEFICIT"
        assert flags[0].severity == FlagSeverity.WARNING

    def test_five_consecutive_is_critical(self):
        snapshots = [self._snap_net(2024, m, -500) for m in range(1, 6)]
        flags = _check_sustained_deficit(snapshots)
        assert flags[0].severity == FlagSeverity.CRITICAL

    def test_two_consecutive_no_flag(self):
        snapshots = [self._snap_net(2024, m, -500) for m in range(1, 3)]
        flags = _check_sustained_deficit(snapshots)
        assert flags == []

    def test_broken_streak_resets(self):
        snapshots = [
            self._snap_net(2024, 1, -500),
            self._snap_net(2024, 2, -500),
            self._snap_net(2024, 3, 500),    # recovery — resets streak
            self._snap_net(2024, 4, -500),
            self._snap_net(2024, 5, -500),
        ]
        flags = _check_sustained_deficit(snapshots)
        assert flags == []

    def test_streak_ending_at_last_month_still_detected(self):
        snapshots = (
            [self._snap_net(2024, 1, 500)]
            + [self._snap_net(2024, m, -500) for m in range(2, 6)]
        )
        flags = _check_sustained_deficit(snapshots)
        assert len(flags) == 1


# ---------------------------------------------------------------------------
# Full detect() integration
# ---------------------------------------------------------------------------

class TestDetectIntegration:
    def test_clean_business_no_flags(self):
        txs = healthy_txs()
        result = score(txs)
        flags = detect(result, txs)
        assert flags == []

    def test_output_sorted_critical_first(self):
        snapshots = [
            snap(2024, 1, revenue=10000, inter=8000, total_debits=8000),   # CRITICAL round-trip
            snap(2024, 2, revenue=10000, inter=4500, total_debits=4500),   # WARNING round-trip
        ]
        result = make_result(snapshots)
        flags = detect(result, [])
        severities = [f.severity for f in flags]
        # All CRITICALs before all WARNINGs
        seen_non_critical = False
        for s in severities:
            if s != FlagSeverity.CRITICAL:
                seen_non_critical = True
            if seen_non_critical and s == FlagSeverity.CRITICAL:
                pytest.fail("CRITICAL flag appears after non-CRITICAL flag")

    def test_empty_result_returns_empty(self):
        from app.services.scorer import _empty_result
        flags = detect(_empty_result(), [])
        assert flags == []

    def test_returns_list_of_detected_flags(self):
        txs = []
        for m in range(1, 7):
            txs.append(make_tx(2024, m, 5000, C, CAT.PRIMARY_REVENUE))
            txs.append(make_tx(2024, m, 3000, D, CAT.PAYROLL))
            txs.append(make_tx(2024, m, 4000, D, CAT.INTER_ACCOUNT))
        result = score(txs)
        flags = detect(result, txs)
        assert all(isinstance(f, DetectedFlag) for f in flags)

    def test_anomalous_profile_produces_flags(self):
        # Revenue with large matching inter-account transfers every month
        txs = []
        for m in range(1, 7):
            txs.append(make_tx(2024, m, 10_000, C, CAT.PRIMARY_REVENUE))
            txs.append(make_tx(2024, m, 8_000, D, CAT.INTER_ACCOUNT))
            txs.append(make_tx(2024, m, 3_000, D, CAT.PAYROLL))
        result = score(txs)
        flags = detect(result, txs)
        flag_types = {f.flag_type for f in flags}
        assert "ROUND_TRIP_REVENUE" in flag_types
        assert "HIGH_INTER_ACCOUNT" in flag_types

    def test_stressed_profile_produces_deficit_flag(self):
        txs = []
        for m in range(1, 7):
            txs.append(make_tx(2024, m, 4_000, C, CAT.PRIMARY_REVENUE))
            txs.append(make_tx(2024, m, 3_000, D, CAT.PAYROLL))
            txs.append(make_tx(2024, m, 1_500, D, CAT.COMMERCIAL_RENT))
            txs.append(make_tx(2024, m, 500,   D, CAT.GOVERNMENT_VAT))
        result = score(txs)
        flags = detect(result, txs)
        flag_types = {f.flag_type for f in flags}
        assert "SUSTAINED_DEFICIT" in flag_types

    def test_flag_descriptions_are_non_empty(self):
        txs = []
        for m in range(1, 7):
            txs.append(make_tx(2024, m, 10_000, C, CAT.PRIMARY_REVENUE))
            txs.append(make_tx(2024, m, 8_000, D, CAT.INTER_ACCOUNT))
        result = score(txs)
        flags = detect(result, txs)
        for f in flags:
            assert f.description.strip() != ""
            assert f.flag_type.strip() != ""
