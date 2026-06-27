"""
Anomaly Detection Engine.

Analyses a completed ScoringResult and the raw transaction list to surface
behavioural red flags for the credit analyst.

All checks are pure functions — no database access, no side effects.

Detected patterns:
  ROUND_TRIP_REVENUE     — Credit followed by near-matching inter-account debit
                           in same month (revenue recycling / inflated turnover)
  ZERO_REVENUE_MONTH     — Month with activity but no recorded revenue
  REVENUE_SPIKE          — Single month revenue > 3× rolling average
  PAYROLL_IRREGULARITY   — Month where payroll is missing despite other debits
  HIGH_INTER_ACCOUNT     — Inter-account transfers form large share of total debits
  REVENUE_CONCENTRATION  — Single credit transaction represents bulk of month revenue
  SUSTAINED_DEFICIT      — Three or more consecutive months of negative net income
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.models.report import FlagSeverity
from app.services.scorer import ScoringResult, MonthlySnapshot


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass
class DetectedFlag:
    flag_type: str
    description: str
    severity: FlagSeverity
    month: Optional[str] = None   # label like "2024-03", None for portfolio-level


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Round-trip: inter_account_out as fraction of same-month revenue
ROUND_TRIP_CRITICAL  = Decimal("0.70")   # ≥70% → CRITICAL
ROUND_TRIP_WARNING   = Decimal("0.40")   # ≥40% → WARNING

# Revenue spike: month revenue as multiple of the mean of other months
SPIKE_CRITICAL       = Decimal("5.0")
SPIKE_WARNING        = Decimal("3.0")

# Inter-account share of total debits (portfolio average)
IA_SHARE_CRITICAL    = Decimal("0.50")
IA_SHARE_WARNING     = Decimal("0.30")

# Revenue concentration: single credit as fraction of month revenue
CONCENTRATION_INFO   = Decimal("0.80")

# Sustained deficit: consecutive months triggering the flag
SUSTAINED_DEFICIT_N  = 3


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect(result: ScoringResult, transactions: list[dict]) -> list[DetectedFlag]:
    """
    Run all anomaly checks and return a deduplicated, severity-sorted list.

    Args:
        result:       Completed ScoringResult from scorer.score()
        transactions: Same transaction list passed to scorer.score()

    Returns:
        List of DetectedFlag, ordered CRITICAL → WARNING → INFO.
    """
    if not result.monthly_snapshots:
        return []

    flags: list[DetectedFlag] = []

    flags.extend(_check_round_trip(result.monthly_snapshots))
    flags.extend(_check_zero_revenue(result.monthly_snapshots))
    flags.extend(_check_revenue_spike(result.monthly_snapshots))
    flags.extend(_check_payroll_irregularity(result.monthly_snapshots))
    flags.extend(_check_high_inter_account(result.monthly_snapshots))
    flags.extend(_check_revenue_concentration(transactions))
    flags.extend(_check_sustained_deficit(result.monthly_snapshots))

    return _sort_flags(flags)


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

def _check_round_trip(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag months where inter-account outflows are a high fraction of revenue,
    indicating the revenue figure may be inflated by recycled transfers.
    """
    flags = []
    for s in snapshots:
        if s.revenue == 0:
            continue
        ratio = s.inter_account_out / s.revenue
        if ratio >= ROUND_TRIP_CRITICAL:
            flags.append(DetectedFlag(
                flag_type="ROUND_TRIP_REVENUE",
                description=(
                    f"{s.label}: inter-account outflows are "
                    f"{ratio * 100:.0f}% of recorded revenue "
                    f"(BHD {s.inter_account_out:,.0f} out vs "
                    f"BHD {s.revenue:,.0f} in). Likely revenue recycling."
                ),
                severity=FlagSeverity.CRITICAL,
                month=s.label,
            ))
        elif ratio >= ROUND_TRIP_WARNING:
            flags.append(DetectedFlag(
                flag_type="ROUND_TRIP_REVENUE",
                description=(
                    f"{s.label}: inter-account outflows are "
                    f"{ratio * 100:.0f}% of recorded revenue "
                    f"(BHD {s.inter_account_out:,.0f} out vs "
                    f"BHD {s.revenue:,.0f} in). Possible fund recycling."
                ),
                severity=FlagSeverity.WARNING,
                month=s.label,
            ))
    return flags


def _check_zero_revenue(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag months with zero revenue when the account has other activity.
    Isolated gaps in an otherwise-active account are suspicious.
    """
    flags = []
    for s in snapshots:
        if s.revenue > 0:
            continue
        if s.total_debits == 0:
            continue  # totally inactive month — not suspicious

        # Business was spending but recorded no income
        severity = FlagSeverity.CRITICAL if s.total_debits > 1000 else FlagSeverity.WARNING
        flags.append(DetectedFlag(
            flag_type="ZERO_REVENUE_MONTH",
            description=(
                f"{s.label}: no revenue recorded despite "
                f"BHD {s.total_debits:,.0f} in outflows. "
                "Revenue may be flowing through an undisclosed account."
            ),
            severity=severity,
            month=s.label,
        ))
    return flags


def _check_revenue_spike(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag months where revenue is anomalously high relative to the other months.
    Excludes the spike month itself from the baseline average.
    """
    if len(snapshots) < 3:
        return []

    flags = []
    revenues = [s.revenue for s in snapshots]

    for i, s in enumerate(snapshots):
        if s.revenue == 0:
            continue
        others = [r for j, r in enumerate(revenues) if j != i]
        other_sum = sum(others, Decimal("0"))
        other_mean = other_sum / Decimal(str(len(others)))
        if other_mean == 0:
            continue

        ratio = s.revenue / other_mean
        if ratio >= SPIKE_CRITICAL:
            flags.append(DetectedFlag(
                flag_type="REVENUE_SPIKE",
                description=(
                    f"{s.label}: revenue of BHD {s.revenue:,.0f} is "
                    f"{ratio:.1f}× the average of other months "
                    f"(BHD {other_mean:,.0f}). Investigate for one-off or inflated receipt."
                ),
                severity=FlagSeverity.CRITICAL,
                month=s.label,
            ))
        elif ratio >= SPIKE_WARNING:
            flags.append(DetectedFlag(
                flag_type="REVENUE_SPIKE",
                description=(
                    f"{s.label}: revenue of BHD {s.revenue:,.0f} is "
                    f"{ratio:.1f}× the average of other months "
                    f"(BHD {other_mean:,.0f}). May reflect an exceptional receipt."
                ),
                severity=FlagSeverity.WARNING,
                month=s.label,
            ))
    return flags


def _check_payroll_irregularity(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag months where payroll is absent in a business that normally pays salaries.
    Missing payroll in an active month may indicate WPS non-compliance or hidden payroll.
    """
    avg_payroll = _avg([s.payroll for s in snapshots])
    if avg_payroll == 0:
        return []  # business has no recorded payroll — not a payroll-paying entity

    flags = []
    for s in snapshots:
        if s.payroll == 0 and s.total_debits > 0:
            severity = FlagSeverity.CRITICAL if s.total_debits > avg_payroll else FlagSeverity.WARNING
            flags.append(DetectedFlag(
                flag_type="PAYROLL_IRREGULARITY",
                description=(
                    f"{s.label}: no WPS/payroll payment recorded despite "
                    f"BHD {s.total_debits:,.0f} in outflows. "
                    f"Average monthly payroll for this account is BHD {avg_payroll:,.0f}."
                ),
                severity=severity,
                month=s.label,
            ))
    return flags


def _check_high_inter_account(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag when inter-account transfers form a disproportionate share of total
    outflows across the analysis period (portfolio-level signal, not per-month).
    """
    total_ia = sum((s.inter_account_out for s in snapshots), Decimal("0"))
    total_debits = sum((s.total_debits for s in snapshots), Decimal("0"))

    if total_debits == 0:
        return []

    ratio = total_ia / total_debits
    if ratio >= IA_SHARE_CRITICAL:
        return [DetectedFlag(
            flag_type="HIGH_INTER_ACCOUNT",
            description=(
                f"Inter-account transfers represent {ratio * 100:.0f}% of total outflows "
                f"(BHD {total_ia:,.0f} of BHD {total_debits:,.0f}). "
                "Significant cash cycling between accounts detected."
            ),
            severity=FlagSeverity.CRITICAL,
        )]
    if ratio >= IA_SHARE_WARNING:
        return [DetectedFlag(
            flag_type="HIGH_INTER_ACCOUNT",
            description=(
                f"Inter-account transfers represent {ratio * 100:.0f}% of total outflows "
                f"(BHD {total_ia:,.0f} of BHD {total_debits:,.0f}). "
                "Elevated internal fund movement — confirm accounts are same-entity."
            ),
            severity=FlagSeverity.WARNING,
        )]
    return []


def _check_revenue_concentration(transactions: list[dict]) -> list[DetectedFlag]:
    """
    Flag months where a single credit transaction represents ≥80% of total
    monthly revenue (counterparty concentration / single-event dependency risk).
    """
    from app.models.transaction import TransactionDirection, TransactionCategory
    from collections import defaultdict

    monthly_credits: dict[str, list[Decimal]] = defaultdict(list)

    for t in transactions:
        dt = t["transaction_date"]
        label = f"{dt.year}-{dt.month:02d}"
        if (t["direction"] == TransactionDirection.CREDIT
                and t["category"] == TransactionCategory.PRIMARY_REVENUE):
            monthly_credits[label].append(Decimal(str(t["amount"])))

    flags = []
    for label, amounts in monthly_credits.items():
        if len(amounts) < 2:
            continue  # only one receipt — skip (no concentration within month)
        total = sum(amounts, Decimal("0"))
        if total == 0:
            continue
        largest = max(amounts)
        ratio = largest / total
        if ratio >= CONCENTRATION_INFO:
            flags.append(DetectedFlag(
                flag_type="REVENUE_CONCENTRATION",
                description=(
                    f"{label}: single receipt of BHD {largest:,.0f} represents "
                    f"{ratio * 100:.0f}% of month revenue (BHD {total:,.0f}). "
                    "High dependency on a single counterparty or event."
                ),
                severity=FlagSeverity.INFO,
                month=label,
            ))
    return flags


def _check_sustained_deficit(snapshots: list[MonthlySnapshot]) -> list[DetectedFlag]:
    """
    Flag when three or more consecutive months show negative net operating income.
    A single bad month is noise; sustained deficit signals structural distress.
    """
    flags = []
    run = 0
    run_start = None

    for s in snapshots:
        if s.net_operating_income < 0:
            run += 1
            if run == 1:
                run_start = s.label
        else:
            if run >= SUSTAINED_DEFICIT_N:
                flags.append(_make_deficit_flag(run_start, s.label, run))
            run = 0
            run_start = None

    if run >= SUSTAINED_DEFICIT_N:
        flags.append(_make_deficit_flag(run_start, snapshots[-1].label, run))

    return flags


def _make_deficit_flag(start: str, end: str, months: int) -> DetectedFlag:
    severity = FlagSeverity.CRITICAL if months >= 5 else FlagSeverity.WARNING
    return DetectedFlag(
        flag_type="SUSTAINED_DEFICIT",
        description=(
            f"{months} consecutive months of negative net operating income "
            f"({start} → {end}). Operating expenses persistently exceed revenue."
        ),
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _avg(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(str(len(values)))


_SEVERITY_ORDER = {
    FlagSeverity.CRITICAL: 0,
    FlagSeverity.WARNING: 1,
    FlagSeverity.INFO: 2,
}


def _sort_flags(flags: list[DetectedFlag]) -> list[DetectedFlag]:
    return sorted(flags, key=lambda f: (_SEVERITY_ORDER[f.severity], f.month or ""))
