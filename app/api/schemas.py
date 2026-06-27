"""
Pydantic request/response schemas for the GCC Underwriting API.

Kept separate from SQLAlchemy models so the API contract is independent
of database internals.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class TransactionIn(BaseModel):
    transaction_date: date
    amount: Decimal = Field(..., gt=0)
    direction: str = Field(..., pattern="^(credit|debit)$")
    raw_description: str = Field(..., max_length=500)
    currency: str = Field(default="BHD", max_length=3)
    raw_json: Optional[dict] = None

    @field_validator("raw_description")
    @classmethod
    def strip_description(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class AssessmentRequest(BaseModel):
    sme_external_ref: str = Field(..., max_length=100, description="Bank's own ID for this SME")
    business_name: str = Field(..., max_length=255)
    transactions: list[TransactionIn] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class FlagOut(BaseModel):
    flag_type: str
    description: str
    severity: str
    model_config = {"from_attributes": True}


class MonthlySnapshotOut(BaseModel):
    period: str
    revenue: str
    payroll: str
    rent: str
    government_vat: str
    capex: str
    bank_charges: str
    inter_account_out: str
    total_debits: str
    net_operating_income: str


class AssessmentReportOut(BaseModel):
    assessment_id: UUID
    sme_external_ref: str
    business_name: str
    status: str

    # Period
    analysis_months: int
    start_period: str
    end_period: str

    # Revenue
    avg_monthly_revenue: Decimal
    total_revenue: Decimal
    revenue_volatility_index: Decimal
    revenue_trend_pct: Decimal
    revenue_trend_label: str

    # DSCR
    dscr: Decimal
    dscr_label: str

    # Expenses
    avg_monthly_payroll: Decimal
    avg_monthly_rent: Decimal
    avg_monthly_government_vat: Decimal
    avg_monthly_capex: Decimal
    avg_monthly_bank_charges: Decimal
    avg_monthly_total_expenses: Decimal
    avg_monthly_fixed_obligations: Decimal
    avg_monthly_net: Decimal

    # Burn
    burn_rate_monthly: Decimal
    monthly_surplus_deficit: Decimal
    runway_months: Optional[Decimal]

    # Risk
    risk_score: int
    risk_level: str

    # Flags
    flag_count: int
    flags: list[FlagOut]

    # Monthly detail
    monthly_snapshots: list[MonthlySnapshotOut]


class AssessmentStatusOut(BaseModel):
    assessment_id: UUID
    status: str
    created_at: str
    completed_at: Optional[str]


class ErrorOut(BaseModel):
    detail: str
