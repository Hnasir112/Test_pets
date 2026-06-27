"""
Assessment endpoints.

POST /v1/assessments            — submit transactions, run pipeline, return report
GET  /v1/assessments/{id}       — retrieve a completed report by assessment ID
GET  /v1/assessments/{id}/flags — list anomaly flags for an assessment
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_institution
from app.api.schemas import (
    AssessmentRequest,
    AssessmentReportOut,
    FlagOut,
    MonthlySnapshotOut,
)
from app.db.base import get_db
from app.models.assessment import Assessment, AssessmentStatus
from app.models.institution import Institution
from app.models.report import AssessmentReport, AnomalyFlag
from app.models.sme_profile import SmeProfile
from app.services.pipeline import run_assessment, AssessmentNotFoundError

router = APIRouter(prefix="/v1/assessments", tags=["assessments"])


# ---------------------------------------------------------------------------
# POST /v1/assessments
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=AssessmentReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit transactions and run underwriting assessment",
)
def create_assessment(
    body: AssessmentRequest,
    institution: Institution = Depends(get_institution),
    db: Session = Depends(get_db),
) -> AssessmentReportOut:
    """
    Submit a batch of bank transactions for an SME and receive a full
    credit scorecard in response.

    The call is synchronous — it returns only after the pipeline has
    completed (typically under 2 seconds for 12 months of data).

    The `sme_external_ref` is your institution's own identifier for this
    business. If the SME is new, a profile is created automatically.
    """
    # Resolve or create SME profile
    sme = (
        db.query(SmeProfile)
        .filter_by(institution_id=institution.id, external_ref=body.sme_external_ref)
        .first()
    )
    if sme is None:
        sme = SmeProfile(
            institution_id=institution.id,
            external_ref=body.sme_external_ref,
            business_name=body.business_name,
        )
        db.add(sme)
        db.flush()
    else:
        # Update business name if it changed
        if sme.business_name != body.business_name:
            sme.business_name = body.business_name

    # Create assessment record
    assessment = Assessment(
        institution_id=institution.id,
        sme_profile_id=sme.id,
        status=AssessmentStatus.PENDING,
    )
    db.add(assessment)
    db.flush()

    # Convert request transactions to pipeline-compatible dicts
    raw_txs = [
        {
            "transaction_date": t.transaction_date,
            "amount": t.amount,
            "direction": t.direction,
            "raw_description": t.raw_description,
            "currency": t.currency,
            "raw_json": t.raw_json,
        }
        for t in body.transactions
    ]

    # Run the full pipeline
    report = run_assessment(db, assessment.id, raw_txs)
    db.commit()
    db.refresh(report)

    flags = db.query(AnomalyFlag).filter_by(assessment_id=assessment.id).all()

    return _build_response(assessment, sme, report, flags)


# ---------------------------------------------------------------------------
# GET /v1/assessments/{id}
# ---------------------------------------------------------------------------

@router.get(
    "/{assessment_id}",
    response_model=AssessmentReportOut,
    summary="Retrieve a completed assessment report",
)
def get_assessment(
    assessment_id: UUID,
    institution: Institution = Depends(get_institution),
    db: Session = Depends(get_db),
) -> AssessmentReportOut:
    """
    Retrieve the full report for a previously submitted assessment.

    Returns 404 if the assessment does not exist or belongs to a different
    institution.
    """
    assessment = db.get(Assessment, assessment_id)
    _check_ownership(assessment, institution)

    if assessment.status != AssessmentStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Assessment is {assessment.status.value} — report not yet available",
        )

    report = db.query(AssessmentReport).filter_by(assessment_id=assessment_id).first()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    flags = db.query(AnomalyFlag).filter_by(assessment_id=assessment_id).all()
    sme = db.get(SmeProfile, assessment.sme_profile_id)

    return _build_response(assessment, sme, report, flags)


# ---------------------------------------------------------------------------
# GET /v1/assessments/{id}/flags
# ---------------------------------------------------------------------------

@router.get(
    "/{assessment_id}/flags",
    response_model=list[FlagOut],
    summary="List anomaly flags for an assessment",
)
def get_flags(
    assessment_id: UUID,
    institution: Institution = Depends(get_institution),
    db: Session = Depends(get_db),
) -> list[FlagOut]:
    """
    Return the anomaly flags detected during underwriting, ordered by
    severity (CRITICAL → WARNING → INFO).
    """
    assessment = db.get(Assessment, assessment_id)
    _check_ownership(assessment, institution)

    flags = db.query(AnomalyFlag).filter_by(assessment_id=assessment_id).all()
    return [FlagOut(flag_type=f.flag_type, description=f.description, severity=f.severity.value)
            for f in flags]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_ownership(assessment: Optional[Assessment], institution: Institution) -> None:
    if assessment is None or assessment.institution_id != institution.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")


def _build_response(
    assessment: Assessment,
    sme: SmeProfile,
    report: AssessmentReport,
    flags: list[AnomalyFlag],
) -> AssessmentReportOut:
    rj = report.report_json  # already-computed dict from pipeline

    snapshots = [
        MonthlySnapshotOut(
            period=s["period"],
            revenue=s["revenue"],
            payroll=s["payroll"],
            rent=s["rent"],
            government_vat=s["government_vat"],
            capex=s["capex"],
            bank_charges=s["bank_charges"],
            inter_account_out=s["inter_account_out"],
            total_debits=s["total_debits"],
            net_operating_income=s["net_operating_income"],
        )
        for s in rj.get("monthly_snapshots", [])
    ]

    flag_outs = [
        FlagOut(
            flag_type=f.flag_type,
            description=f.description,
            severity=f.severity.value,
        )
        for f in flags
    ]

    expenses = rj.get("expenses", {})
    net = rj.get("net", {})
    burn = rj.get("burn", {})
    risk = rj.get("risk", {})

    return AssessmentReportOut(
        assessment_id=assessment.id,
        sme_external_ref=sme.external_ref,
        business_name=sme.business_name,
        status=assessment.status.value,

        analysis_months=rj.get("analysis_months", 0),
        start_period=rj.get("start_period", "N/A"),
        end_period=rj.get("end_period", "N/A"),

        avg_monthly_revenue=Decimal(rj["revenue"]["avg_monthly"]),
        total_revenue=Decimal(rj["revenue"]["total"]),
        revenue_volatility_index=Decimal(rj["revenue"]["volatility_index"]),
        revenue_trend_pct=Decimal(rj["revenue"]["trend_pct"]),
        revenue_trend_label=rj["revenue"]["trend_label"],

        dscr=Decimal(rj["dscr"]["value"]),
        dscr_label=rj["dscr"]["label"],

        avg_monthly_payroll=Decimal(expenses.get("avg_monthly_payroll", "0")),
        avg_monthly_rent=Decimal(expenses.get("avg_monthly_rent", "0")),
        avg_monthly_government_vat=Decimal(expenses.get("avg_monthly_government_vat", "0")),
        avg_monthly_capex=Decimal(expenses.get("avg_monthly_capex", "0")),
        avg_monthly_bank_charges=Decimal(expenses.get("avg_monthly_bank_charges", "0")),
        avg_monthly_total_expenses=Decimal(expenses.get("avg_monthly_total", "0")),
        avg_monthly_fixed_obligations=Decimal(expenses.get("avg_monthly_fixed_obligations", "0")),
        avg_monthly_net=Decimal(net.get("avg_monthly", "0")),

        burn_rate_monthly=Decimal(burn.get("burn_rate_monthly", "0")),
        monthly_surplus_deficit=Decimal(net.get("monthly_surplus_deficit", "0")),
        runway_months=Decimal(burn["runway_months"]) if burn.get("runway_months") else None,

        risk_score=risk.get("score", 100),
        risk_level=risk.get("level", "critical"),

        flag_count=len(flag_outs),
        flags=flag_outs,

        monthly_snapshots=snapshots,
    )
