"""
Assessment Pipeline.

Orchestrates the full underwriting workflow for a single assessment:
  1. Mark assessment PROCESSING
  2. Normalize each raw transaction (normalizer)
  3. Persist Transaction rows to the database
  4. Score the normalized transaction set (scorer)
  5. Detect behavioural anomalies (detector)
  6. Persist AssessmentReport and AnomalyFlag rows
  7. Mark assessment COMPLETED (or FAILED on exception)

Entry point:  run_assessment(db, assessment_id, raw_transactions)
Returns:      the persisted AssessmentReport ORM object
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.assessment import Assessment, AssessmentStatus
from app.models.report import AssessmentReport, AnomalyFlag, FlagSeverity
from app.models.transaction import Transaction, TransactionCategory, TransactionDirection
from app.services.normalizer import normalize
from app.services.scorer import score, ScoringResult
from app.services.detector import detect


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AssessmentNotFoundError(Exception):
    pass


class AssessmentStateError(Exception):
    pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_assessment(
    db: Session,
    assessment_id: UUID,
    raw_transactions: list[dict[str, Any]],
) -> AssessmentReport:
    """
    Execute the full underwriting pipeline for one assessment.

    Args:
        db:               Active SQLAlchemy session (caller manages commit/rollback).
        assessment_id:    UUID of an existing Assessment row in PENDING state.
        raw_transactions: List of dicts, each with:
                            transaction_date  (date | datetime)
                            amount            (Decimal | float | str)
                            direction         (TransactionDirection or its string value)
                            raw_description   (str)
                            currency          (str, optional — defaults to "BHD")
                            raw_json          (dict, optional — original bank payload)

    Returns:
        The persisted AssessmentReport ORM object (not yet committed — caller commits).

    Raises:
        AssessmentNotFoundError: if the assessment_id does not exist.
        AssessmentStateError:    if the assessment is not in PENDING status.
        Exception:               any unexpected error — assessment is marked FAILED first.
    """
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        raise AssessmentNotFoundError(f"Assessment {assessment_id} not found")

    if assessment.status != AssessmentStatus.PENDING:
        raise AssessmentStateError(
            f"Assessment {assessment_id} is {assessment.status.value}, expected pending"
        )

    try:
        assessment.status = AssessmentStatus.PROCESSING
        db.flush()

        # Step 1: normalise and persist transactions
        scorer_input = _normalise_and_persist(db, assessment_id, raw_transactions)

        # Step 2: score
        scoring_result = score(scorer_input)

        # Step 3: detect anomalies
        flags = detect(scoring_result, scorer_input)

        # Step 4: persist report
        report = _persist_report(db, assessment_id, scoring_result)

        # Step 5: persist flags
        _persist_flags(db, assessment_id, flags)

        # Step 6: mark complete
        assessment.status = AssessmentStatus.COMPLETED
        assessment.completed_at = datetime.now(timezone.utc)
        db.flush()

        return report

    except Exception:
        assessment.status = AssessmentStatus.FAILED
        db.flush()
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_and_persist(
    db: Session,
    assessment_id: UUID,
    raw_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Normalise every raw transaction, bulk-insert Transaction rows, and return
    the scorer-compatible list (with resolved enums and Decimal amounts).
    """
    scorer_input: list[dict[str, Any]] = []
    orm_rows: list[Transaction] = []

    for raw in raw_transactions:
        direction = _coerce_direction(raw["direction"])
        amount = Decimal(str(raw["amount"]))
        raw_desc = (raw.get("raw_description") or "").strip()
        currency = raw.get("currency", "BHD")
        raw_json = raw.get("raw_json")

        norm = normalize(raw_desc, direction, amount)

        tx_date = raw["transaction_date"]

        orm_rows.append(Transaction(
            assessment_id=assessment_id,
            transaction_date=tx_date,
            raw_description=raw_desc,
            normalized_description=norm.normalized_description,
            amount=amount,
            direction=direction,
            category=norm.category,
            currency=currency,
            raw_json=raw_json,
        ))

        scorer_input.append({
            "transaction_date": tx_date,
            "amount": amount,
            "direction": direction,
            "category": norm.category,
        })

    db.bulk_save_objects(orm_rows)
    return scorer_input


def _persist_report(
    db: Session,
    assessment_id: UUID,
    result: ScoringResult,
) -> AssessmentReport:
    """Build and flush an AssessmentReport from a ScoringResult."""
    report_json = _scoring_result_to_dict(result)

    report = AssessmentReport(
        assessment_id=assessment_id,
        dscr=result.dscr,
        avg_monthly_revenue=result.avg_monthly_revenue,
        avg_monthly_expenses=result.avg_monthly_total_expenses,
        avg_monthly_net=result.avg_monthly_net,
        burn_rate_monthly=result.burn_rate_monthly,
        runway_months=result.runway_months,
        revenue_volatility_index=result.revenue_volatility_index,
        risk_score=result.risk_score,
        risk_level=result.risk_level,
        report_json=report_json,
    )
    db.add(report)
    db.flush()
    return report


def _persist_flags(
    db: Session,
    assessment_id: UUID,
    flags,
) -> None:
    """Insert AnomalyFlag rows for all detected flags."""
    orm_flags = [
        AnomalyFlag(
            assessment_id=assessment_id,
            flag_type=f.flag_type,
            description=f.description,
            severity=f.severity,
        )
        for f in flags
    ]
    if orm_flags:
        db.bulk_save_objects(orm_flags)


def _coerce_direction(value: Any) -> TransactionDirection:
    if isinstance(value, TransactionDirection):
        return value
    return TransactionDirection(str(value).lower())


def _scoring_result_to_dict(result: ScoringResult) -> dict:
    """Serialise ScoringResult to a JSON-safe dict for the report_json column."""
    return {
        "analysis_months": result.analysis_months,
        "start_period": result.start_period,
        "end_period": result.end_period,
        "revenue": {
            "avg_monthly": str(result.avg_monthly_revenue),
            "total": str(result.total_revenue),
            "volatility_index": str(result.revenue_volatility_index),
            "trend_pct": str(result.revenue_trend_pct),
            "trend_label": result.revenue_trend_label,
        },
        "expenses": {
            "avg_monthly_payroll": str(result.avg_monthly_payroll),
            "avg_monthly_rent": str(result.avg_monthly_rent),
            "avg_monthly_government_vat": str(result.avg_monthly_government_vat),
            "avg_monthly_capex": str(result.avg_monthly_capex),
            "avg_monthly_bank_charges": str(result.avg_monthly_bank_charges),
            "avg_monthly_total": str(result.avg_monthly_total_expenses),
            "avg_monthly_fixed_obligations": str(result.avg_monthly_fixed_obligations),
        },
        "net": {
            "avg_monthly": str(result.avg_monthly_net),
            "monthly_surplus_deficit": str(result.monthly_surplus_deficit),
        },
        "dscr": {
            "value": str(result.dscr),
            "label": result.dscr_label,
        },
        "burn": {
            "burn_rate_monthly": str(result.burn_rate_monthly),
            "runway_months": str(result.runway_months) if result.runway_months is not None else None,
        },
        "risk": {
            "score": result.risk_score,
            "level": result.risk_level.value,
        },
        "anomaly_hints": {
            "zero_revenue_months": result.zero_revenue_months,
            "negative_net_months": result.negative_net_months,
        },
        "monthly_snapshots": [
            {
                "period": s.label,
                "revenue": str(s.revenue),
                "payroll": str(s.payroll),
                "rent": str(s.rent),
                "government_vat": str(s.government_vat),
                "capex": str(s.capex),
                "bank_charges": str(s.bank_charges),
                "inter_account_out": str(s.inter_account_out),
                "total_debits": str(s.total_debits),
                "net_operating_income": str(s.net_operating_income),
            }
            for s in result.monthly_snapshots
        ],
    }
