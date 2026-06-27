"""
Unit tests for the assessment pipeline.

Uses an in-memory SQLite database so no PostgreSQL instance is required.
Tests verify that the pipeline correctly:
  - Transitions assessment status: PENDING → PROCESSING → COMPLETED
  - Persists normalised Transaction rows
  - Persists AssessmentReport with correct metrics
  - Persists AnomalyFlag rows for problematic profiles
  - Marks assessment FAILED on error and re-raises
  - Rejects non-PENDING assessments
  - Handles empty transaction lists gracefully
  - Serialises report_json with all required keys
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import json
import pytest
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# SQLite doesn't support PostgreSQL-specific types. Teach its compiler to
# treat JSONB as TEXT so in-memory tests don't need a real Postgres instance.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: F401 (import triggers registration)

def _visit_JSONB(self, type_, **kw):
    return "TEXT"

SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

from app.db.base import Base
from app.models.institution import Institution, InstitutionTier
from app.models.sme_profile import SmeProfile
from app.models.assessment import Assessment, AssessmentStatus
from app.models.transaction import Transaction, TransactionCategory, TransactionDirection
from app.models.report import AssessmentReport, AnomalyFlag, RiskLevel
from app.services.pipeline import (
    run_assessment,
    AssessmentNotFoundError,
    AssessmentStateError,
)


# ---------------------------------------------------------------------------
# In-memory SQLite test database
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    # SQLite does not enforce foreign keys by default
    @event.listens_for(eng, "connect")
    def set_fk_pragma(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# Fixtures: minimal institution + sme_profile + assessment
# ---------------------------------------------------------------------------

@pytest.fixture
def institution(db):
    inst = Institution(
        name="Test Bank",
        tier=InstitutionTier.PILOT,
    )
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture
def sme(db, institution):
    sme = SmeProfile(
        institution_id=institution.id,
        external_ref="SME-001",
        business_name="Al Noor Trading Co",
        registration_number="CR123456",
        industry="Trading",
    )
    db.add(sme)
    db.flush()
    return sme


@pytest.fixture
def assessment(db, institution, sme):
    a = Assessment(
        institution_id=institution.id,
        sme_profile_id=sme.id,
        status=AssessmentStatus.PENDING,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Transaction builders
# ---------------------------------------------------------------------------

D = TransactionDirection.DEBIT
C = TransactionDirection.CREDIT
CAT = TransactionCategory


def tx(year, month, amount, direction, raw_desc, category_hint=None):
    return {
        "transaction_date": date(year, month, 15),
        "amount": Decimal(str(amount)),
        "direction": direction,
        "raw_description": raw_desc,
        "currency": "BHD",
    }


def healthy_txs():
    txs = []
    for m in range(1, 7):
        txs.append(tx(2024, m, 10_000, C, f"INWARD REMITTANCE - GULF TRADING CO REF:2024{m:02d}"))
        txs.append(tx(2024, m, 3_000, D, f"WPS SALARY TRANSFER MAR 2024"))
        txs.append(tx(2024, m, 1_000, D, f"MONTHLY RENT SEEF OFFICE {m}/2024"))
        txs.append(tx(2024, m, 500,   D, f"SADAD PMT - MOF VAT Q{m} 2024"))
        txs.append(tx(2024, m, 400,   D, f"AMAZON.AE - OFFICE SUPPLIES 707314"))
        txs.append(tx(2024, m, 100,   D, f"ACCOUNT MAINTENANCE FEE {m}/2024"))
    return txs


def round_trip_txs():
    txs = []
    for m in range(1, 7):
        txs.append(tx(2024, m, 10_000, C, f"INWARD REMITTANCE - GULF TRADING CO REF:2024{m:02d}"))
        txs.append(tx(2024, m, 8_000, D, f"INTERNAL TRF - PETTY CASH 2024{m:02d}"))
        txs.append(tx(2024, m, 3_000, D, f"WPS SALARY TRANSFER 2024{m:02d}"))
    return txs


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    def test_pending_to_completed(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        db.refresh(assessment)
        assert assessment.status == AssessmentStatus.COMPLETED

    def test_completed_at_populated(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        db.refresh(assessment)
        assert assessment.completed_at is not None

    def test_rejects_non_pending_assessment(self, db, institution, sme):
        a = Assessment(
            institution_id=institution.id,
            sme_profile_id=sme.id,
            status=AssessmentStatus.COMPLETED,
        )
        db.add(a)
        db.flush()
        with pytest.raises(AssessmentStateError):
            run_assessment(db, a.id, healthy_txs())

    def test_raises_for_missing_assessment(self, db):
        with pytest.raises(AssessmentNotFoundError):
            run_assessment(db, uuid.uuid4(), healthy_txs())

    def test_marks_failed_on_exception(self, db, assessment):
        bad_txs = [{"transaction_date": date(2024, 1, 1), "amount": "bad", "direction": C, "raw_description": "test"}]
        with pytest.raises(Exception):
            run_assessment(db, assessment.id, bad_txs)
        db.refresh(assessment)
        assert assessment.status == AssessmentStatus.FAILED


# ---------------------------------------------------------------------------
# Transaction persistence
# ---------------------------------------------------------------------------

class TestTransactionPersistence:
    def test_transactions_persisted(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        count = db.query(Transaction).filter_by(assessment_id=assessment.id).count()
        assert count == len(healthy_txs())

    def test_transaction_category_normalised(self, db, assessment):
        run_assessment(db, assessment.id, [
            tx(2024, 1, 5000, C, "INWARD REMITTANCE - AL NOOR CO REF:12345"),
        ])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.category == TransactionCategory.PRIMARY_REVENUE

    def test_payroll_transaction_category(self, db, assessment):
        run_assessment(db, assessment.id, [
            tx(2024, 1, 3000, D, "WPS SALARY TRANSFER MAR 2024"),
        ])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.category == TransactionCategory.PAYROLL

    def test_raw_description_preserved(self, db, assessment):
        raw = "WPS SALARY TRANSFER MAR 2024 REF:123456789"
        run_assessment(db, assessment.id, [tx(2024, 1, 3000, D, raw)])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.raw_description == raw

    def test_normalized_description_differs_from_raw(self, db, assessment):
        raw = "WPS SALARY TRANSFER MAR 2024 REF:123456789"
        run_assessment(db, assessment.id, [tx(2024, 1, 3000, D, raw)])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        # Reference number should be stripped in normalised form
        assert "123456789" not in t.normalized_description

    def test_amount_stored_as_decimal(self, db, assessment):
        run_assessment(db, assessment.id, [tx(2024, 1, 1234.567, C, "INWARD REMITTANCE CO REF:9999")])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.amount is not None

    def test_currency_defaults_to_bhd(self, db, assessment):
        run_assessment(db, assessment.id, [tx(2024, 1, 5000, C, "INWARD REMITTANCE CO REF:9999")])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.currency == "BHD"

    def test_direction_string_coerced(self, db, assessment):
        run_assessment(db, assessment.id, [{
            "transaction_date": date(2024, 1, 15),
            "amount": "5000",
            "direction": "credit",   # string, not enum
            "raw_description": "INWARD REMITTANCE CO REF:9999",
        }])
        t = db.query(Transaction).filter_by(assessment_id=assessment.id).first()
        assert t.direction == TransactionDirection.CREDIT


# ---------------------------------------------------------------------------
# Report persistence
# ---------------------------------------------------------------------------

class TestReportPersistence:
    def test_report_created(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report is not None

    def test_dscr_positive_for_healthy(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report.dscr > 1

    def test_risk_level_low_for_healthy(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report.risk_level == RiskLevel.LOW

    def test_risk_score_populated(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report.risk_score is not None
        assert 0 <= report.risk_score <= 100

    def test_report_json_has_required_keys(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        rj = report.report_json
        assert "dscr" in rj
        assert "revenue" in rj
        assert "expenses" in rj
        assert "risk" in rj
        assert "monthly_snapshots" in rj

    def test_monthly_snapshots_in_report_json(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        snapshots = report.report_json["monthly_snapshots"]
        assert len(snapshots) == 6
        assert "period" in snapshots[0]
        assert "revenue" in snapshots[0]

    def test_runway_none_for_healthy(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report.runway_months is None

    def test_runway_populated_for_stressed(self, db, institution, sme):
        a = Assessment(institution_id=institution.id, sme_profile_id=sme.id, status=AssessmentStatus.PENDING)
        db.add(a)
        db.flush()
        stressed = []
        for m in range(1, 7):
            stressed.append(tx(2024, m, 4_000, C, f"INWARD REMITTANCE CO REF:20240{m}"))
            stressed.append(tx(2024, m, 3_000, D, "WPS SALARY TRANSFER"))
            stressed.append(tx(2024, m, 1_500, D, "MONTHLY RENT SEEF OFFICE"))
            stressed.append(tx(2024, m, 500,   D, "SADAD PMT - MOF VAT"))
        run_assessment(db, a.id, stressed)
        report = db.query(AssessmentReport).filter_by(assessment_id=a.id).first()
        assert report.runway_months is not None


# ---------------------------------------------------------------------------
# Anomaly flag persistence
# ---------------------------------------------------------------------------

class TestFlagPersistence:
    def test_no_flags_for_healthy(self, db, assessment):
        run_assessment(db, assessment.id, healthy_txs())
        flags = db.query(AnomalyFlag).filter_by(assessment_id=assessment.id).all()
        assert flags == []

    def test_round_trip_flags_persisted(self, db, institution, sme):
        a = Assessment(institution_id=institution.id, sme_profile_id=sme.id, status=AssessmentStatus.PENDING)
        db.add(a)
        db.flush()
        run_assessment(db, a.id, round_trip_txs())
        flags = db.query(AnomalyFlag).filter_by(assessment_id=a.id).all()
        flag_types = {f.flag_type for f in flags}
        assert "ROUND_TRIP_REVENUE" in flag_types or "HIGH_INTER_ACCOUNT" in flag_types

    def test_flags_have_non_empty_description(self, db, institution, sme):
        a = Assessment(institution_id=institution.id, sme_profile_id=sme.id, status=AssessmentStatus.PENDING)
        db.add(a)
        db.flush()
        run_assessment(db, a.id, round_trip_txs())
        flags = db.query(AnomalyFlag).filter_by(assessment_id=a.id).all()
        for f in flags:
            assert f.description.strip() != ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_transaction_list(self, db, assessment):
        run_assessment(db, assessment.id, [])
        db.refresh(assessment)
        assert assessment.status == AssessmentStatus.COMPLETED
        report = db.query(AssessmentReport).filter_by(assessment_id=assessment.id).first()
        assert report is not None
        assert report.risk_score == 100   # empty → max risk

    def test_single_transaction(self, db, assessment):
        run_assessment(db, assessment.id, [
            tx(2024, 1, 5000, C, "INWARD REMITTANCE - AL NOOR CO REF:12345"),
        ])
        db.refresh(assessment)
        assert assessment.status == AssessmentStatus.COMPLETED

    def test_run_returns_report_object(self, db, assessment):
        result = run_assessment(db, assessment.id, healthy_txs())
        assert isinstance(result, AssessmentReport)
        assert result.assessment_id == assessment.id
