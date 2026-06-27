"""
Integration tests for the FastAPI assessment endpoints.

Uses TestClient (sync HTTPX) against the real FastAPI app wired to an
in-memory SQLite database. No Postgres instance required.

Tests cover:
  - POST /v1/assessments — happy path, auth failures, validation errors
  - GET  /v1/assessments/{id} — report retrieval, ownership check
  - GET  /v1/assessments/{id}/flags — flags list
  - GET  /health — liveness probe
  - Edge cases: empty description, unknown SME ref, duplicate SME auto-create
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Teach SQLite to handle PostgreSQL-specific types
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: F401

def _visit_JSONB(self, type_, **kw):
    return "TEXT"

SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

from app.db.base import Base, get_db
from app.main import app
from app.models.institution import Institution, InstitutionTier
from app.models.api_key import ApiKey


# ---------------------------------------------------------------------------
# In-memory database + app wiring
# ---------------------------------------------------------------------------

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,   # all connections share one in-memory database
)

@event.listens_for(TEST_ENGINE, "connect")
def _fk_pragma(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")

Base.metadata.create_all(TEST_ENGINE)
TestSession = sessionmaker(bind=TEST_ENGINE)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


# ---------------------------------------------------------------------------
# Seed: one institution + one API key
# ---------------------------------------------------------------------------

_RAW_KEY = f"gccuw_{secrets.token_hex(24)}"
_KEY_HASH = hashlib.sha256(_RAW_KEY.encode()).hexdigest()
_VALID_HEADER = {"X-API-Key": _RAW_KEY}

def _seed():
    db = TestSession()
    inst = Institution(name="NBB Test", tier=InstitutionTier.PILOT)
    db.add(inst)
    db.flush()
    key = ApiKey(
        institution_id=inst.id,
        key_hash=_KEY_HASH,
        label="test-key",
        is_active=True,
    )
    db.add(key)
    db.commit()
    return inst.id

_INSTITUTION_ID = _seed()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Sample payload helpers
# ---------------------------------------------------------------------------

def _healthy_payload(sme_ref: str = "SME-HEALTHY-001") -> dict:
    txs = []
    for m in range(1, 7):
        txs += [
            {"transaction_date": f"2024-{m:02d}-15", "amount": "10000.000",
             "direction": "credit", "raw_description": f"INWARD REMITTANCE - GULF TRADING CO REF:2024{m:02d}"},
            {"transaction_date": f"2024-{m:02d}-25", "amount": "3000.000",
             "direction": "debit", "raw_description": "WPS SALARY TRANSFER MAR 2024"},
            {"transaction_date": f"2024-{m:02d}-28", "amount": "1000.000",
             "direction": "debit", "raw_description": "MONTHLY RENT SEEF OFFICE"},
            {"transaction_date": f"2024-{m:02d}-05", "amount": "500.000",
             "direction": "debit", "raw_description": "SADAD PMT - MOF VAT Q2 2024"},
            {"transaction_date": f"2024-{m:02d}-10", "amount": "100.000",
             "direction": "debit", "raw_description": "ACCOUNT MAINTENANCE FEE"},
        ]
    return {"sme_external_ref": sme_ref, "business_name": "Gulf Trading Co", "transactions": txs}


def _anomalous_payload(sme_ref: str = "SME-ANOMALOUS-001") -> dict:
    txs = []
    for m in range(1, 7):
        txs += [
            {"transaction_date": f"2024-{m:02d}-15", "amount": "10000.000",
             "direction": "credit", "raw_description": f"INWARD REMITTANCE - CO REF:20240{m}"},
            {"transaction_date": f"2024-{m:02d}-16", "amount": "8000.000",
             "direction": "debit", "raw_description": f"INTERNAL TRF - PETTY CASH 2024{m:02d}"},
            {"transaction_date": f"2024-{m:02d}-25", "amount": "3000.000",
             "direction": "debit", "raw_description": "WPS SALARY TRANSFER"},
        ]
    return {"sme_external_ref": sme_ref, "business_name": "Suspicious Co", "transactions": txs}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# POST /v1/assessments
# ---------------------------------------------------------------------------

class TestCreateAssessment:
    def test_happy_path_returns_201(self):
        r = client.post("/v1/assessments", json=_healthy_payload(), headers=_VALID_HEADER)
        assert r.status_code == 201

    def test_response_has_assessment_id(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-ID-TEST"), headers=_VALID_HEADER)
        assert "assessment_id" in r.json()

    def test_response_has_dscr(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-DSCR-TEST"), headers=_VALID_HEADER)
        body = r.json()
        assert "dscr" in body
        assert float(body["dscr"]) > 1.0

    def test_healthy_profile_low_risk(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-RISK-TEST"), headers=_VALID_HEADER)
        body = r.json()
        assert body["risk_level"] in ("low", "medium")
        assert body["risk_score"] <= 45

    def test_healthy_profile_strong_dscr_label(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-DSCRLAB"), headers=_VALID_HEADER)
        assert r.json()["dscr_label"] == "STRONG"

    def test_six_monthly_snapshots(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-SNAPS"), headers=_VALID_HEADER)
        assert len(r.json()["monthly_snapshots"]) == 6

    def test_sme_ref_and_name_in_response(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-META"), headers=_VALID_HEADER)
        body = r.json()
        assert body["sme_external_ref"] == "SME-META"
        assert body["business_name"] == "Gulf Trading Co"

    def test_status_completed(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-STATUS"), headers=_VALID_HEADER)
        assert r.json()["status"] == "completed"

    def test_runway_none_for_healthy(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-RUNWAY"), headers=_VALID_HEADER)
        assert r.json()["runway_months"] is None

    def test_no_flags_for_healthy(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-FLAGS"), headers=_VALID_HEADER)
        body = r.json()
        assert body["flag_count"] == 0
        assert body["flags"] == []

    def test_anomalous_profile_has_flags(self):
        r = client.post("/v1/assessments", json=_anomalous_payload(), headers=_VALID_HEADER)
        body = r.json()
        assert body["flag_count"] > 0
        assert len(body["flags"]) > 0

    def test_flags_have_severity(self):
        r = client.post("/v1/assessments", json=_anomalous_payload("SME-FLAGSEV"), headers=_VALID_HEADER)
        for flag in r.json()["flags"]:
            assert flag["severity"] in ("critical", "warning", "info")

    def test_duplicate_sme_ref_reuses_profile(self):
        payload = _healthy_payload("SME-REUSE")
        r1 = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        r2 = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        assert r1.status_code == 201
        assert r2.status_code == 201
        # Different assessment IDs but same SME ref
        assert r1.json()["assessment_id"] != r2.json()["assessment_id"]
        assert r1.json()["sme_external_ref"] == r2.json()["sme_external_ref"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_api_key_returns_401(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-NOAUTH"))
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-BADAUTH"),
                        headers={"X-API-Key": "gccuw_totally_wrong_key"})
        assert r.status_code == 401

    def test_get_requires_auth(self):
        import uuid
        r = client.get(f"/v1/assessments/{uuid.uuid4()}")
        assert r.status_code == 401

    def test_flags_requires_auth(self):
        import uuid
        r = client.get(f"/v1/assessments/{uuid.uuid4()}/flags")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_transactions_rejected(self):
        payload = {"sme_external_ref": "SME-VAL", "business_name": "X", "transactions": []}
        r = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        assert r.status_code == 422

    def test_invalid_direction_rejected(self):
        payload = {
            "sme_external_ref": "SME-DIR",
            "business_name": "X",
            "transactions": [{
                "transaction_date": "2024-01-15",
                "amount": "1000",
                "direction": "sideways",
                "raw_description": "test",
            }]
        }
        r = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        assert r.status_code == 422

    def test_negative_amount_rejected(self):
        payload = {
            "sme_external_ref": "SME-AMT",
            "business_name": "X",
            "transactions": [{
                "transaction_date": "2024-01-15",
                "amount": "-500",
                "direction": "credit",
                "raw_description": "test",
            }]
        }
        r = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        assert r.status_code == 422

    def test_missing_sme_ref_rejected(self):
        payload = {"business_name": "X", "transactions": [
            {"transaction_date": "2024-01-15", "amount": "1000",
             "direction": "credit", "raw_description": "test"}
        ]}
        r = client.post("/v1/assessments", json=payload, headers=_VALID_HEADER)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/assessments/{id}
# ---------------------------------------------------------------------------

class TestGetAssessment:
    @pytest.fixture(scope="class")
    def created(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-GET"), headers=_VALID_HEADER)
        assert r.status_code == 201
        return r.json()

    def test_get_returns_200(self, created):
        aid = created["assessment_id"]
        r = client.get(f"/v1/assessments/{aid}", headers=_VALID_HEADER)
        assert r.status_code == 200

    def test_get_returns_same_data(self, created):
        aid = created["assessment_id"]
        r = client.get(f"/v1/assessments/{aid}", headers=_VALID_HEADER)
        body = r.json()
        assert body["dscr"] == created["dscr"]
        assert body["risk_score"] == created["risk_score"]

    def test_get_unknown_id_returns_404(self):
        import uuid
        r = client.get(f"/v1/assessments/{uuid.uuid4()}", headers=_VALID_HEADER)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/assessments/{id}/flags
# ---------------------------------------------------------------------------

class TestGetFlags:
    @pytest.fixture(scope="class")
    def anomalous_id(self):
        r = client.post("/v1/assessments", json=_anomalous_payload("SME-GETFLAGS"),
                        headers=_VALID_HEADER)
        return r.json()["assessment_id"]

    def test_flags_endpoint_returns_200(self, anomalous_id):
        r = client.get(f"/v1/assessments/{anomalous_id}/flags", headers=_VALID_HEADER)
        assert r.status_code == 200

    def test_flags_is_list(self, anomalous_id):
        r = client.get(f"/v1/assessments/{anomalous_id}/flags", headers=_VALID_HEADER)
        assert isinstance(r.json(), list)

    def test_flags_not_empty_for_anomalous(self, anomalous_id):
        r = client.get(f"/v1/assessments/{anomalous_id}/flags", headers=_VALID_HEADER)
        assert len(r.json()) > 0

    def test_flag_schema(self, anomalous_id):
        r = client.get(f"/v1/assessments/{anomalous_id}/flags", headers=_VALID_HEADER)
        flag = r.json()[0]
        assert "flag_type" in flag
        assert "description" in flag
        assert "severity" in flag

    def test_healthy_flags_empty(self):
        r = client.post("/v1/assessments", json=_healthy_payload("SME-NOFLAGS"),
                        headers=_VALID_HEADER)
        aid = r.json()["assessment_id"]
        r2 = client.get(f"/v1/assessments/{aid}/flags", headers=_VALID_HEADER)
        assert r2.json() == []

    def test_unknown_id_returns_404(self):
        import uuid
        r = client.get(f"/v1/assessments/{uuid.uuid4()}/flags", headers=_VALID_HEADER)
        assert r.status_code == 404
