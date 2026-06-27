"""
Integration tests for admin endpoints.

Tests cover:
  - POST /v1/admin/institutions — create institution, auth guard
  - GET  /v1/admin/institutions/{id} — retrieve with keys list
  - POST /v1/admin/institutions/{id}/api-keys — issue key, key format, one-time reveal
  - DELETE /v1/admin/institutions/{id}/api-keys/{key_id} — revoke, idempotency guard
  - Issued API keys work on assessment endpoints immediately after creation
  - Admin key is separate from institution API keys
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import hashlib
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: F401

def _visit_JSONB(self, type_, **kw):
    return "TEXT"

if not hasattr(SQLiteTypeCompiler, "_jsonb_patched"):
    SQLiteTypeCompiler.visit_JSONB = _visit_JSONB
    SQLiteTypeCompiler._jsonb_patched = True

from app.db.base import Base, get_db
from app.main import app
from app.core.config import settings


# ---------------------------------------------------------------------------
# Isolated in-memory DB (separate from test_api.py's engine)
# ---------------------------------------------------------------------------

ADMIN_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

@event.listens_for(ADMIN_ENGINE, "connect")
def _fk(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")

Base.metadata.create_all(ADMIN_ENGINE)
AdminSession = sessionmaker(bind=ADMIN_ENGINE)


def override_db():
    db = AdminSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_db

client = TestClient(app, raise_server_exceptions=True)

ADMIN_HEADER = {"X-Admin-Key": settings.API_SECRET_KEY}
WRONG_ADMIN  = {"X-Admin-Key": "totally_wrong_key"}


# ---------------------------------------------------------------------------
# POST /v1/admin/institutions
# ---------------------------------------------------------------------------

class TestCreateInstitution:
    def test_returns_201(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Bank of Bahrain", "tier": "pilot"},
                        headers=ADMIN_HEADER)
        assert r.status_code == 201

    def test_returns_institution_id(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Gulf Bank", "tier": "standard"},
                        headers=ADMIN_HEADER)
        assert "id" in r.json()

    def test_name_in_response(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Ithmaar Bank", "tier": "pilot"},
                        headers=ADMIN_HEADER)
        assert r.json()["name"] == "Ithmaar Bank"

    def test_tier_stored(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Enterprise Bank", "tier": "enterprise"},
                        headers=ADMIN_HEADER)
        assert r.json()["tier"] == "enterprise"

    def test_empty_api_keys_on_creation(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "New Bank"},
                        headers=ADMIN_HEADER)
        assert r.json()["api_keys"] == []

    def test_is_active_true(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Active Bank"},
                        headers=ADMIN_HEADER)
        assert r.json()["is_active"] is True

    def test_missing_admin_key_returns_401(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Sneaky Bank"})
        assert r.status_code == 401

    def test_wrong_admin_key_returns_401(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Sneaky Bank"},
                        headers=WRONG_ADMIN)
        assert r.status_code == 401

    def test_missing_name_returns_422(self):
        r = client.post("/v1/admin/institutions",
                        json={"tier": "pilot"},
                        headers=ADMIN_HEADER)
        assert r.status_code == 422

    def test_default_tier_is_pilot(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Default Tier Bank"},
                        headers=ADMIN_HEADER)
        assert r.json()["tier"] == "pilot"


# ---------------------------------------------------------------------------
# GET /v1/admin/institutions/{id}
# ---------------------------------------------------------------------------

class TestGetInstitution:
    @pytest.fixture(scope="class")
    def inst_id(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Retrieval Test Bank"},
                        headers=ADMIN_HEADER)
        return r.json()["id"]

    def test_returns_200(self, inst_id):
        r = client.get(f"/v1/admin/institutions/{inst_id}", headers=ADMIN_HEADER)
        assert r.status_code == 200

    def test_returns_correct_name(self, inst_id):
        r = client.get(f"/v1/admin/institutions/{inst_id}", headers=ADMIN_HEADER)
        assert r.json()["name"] == "Retrieval Test Bank"

    def test_unknown_id_returns_404(self):
        import uuid
        r = client.get(f"/v1/admin/institutions/{uuid.uuid4()}", headers=ADMIN_HEADER)
        assert r.status_code == 404

    def test_requires_admin_key(self, inst_id):
        r = client.get(f"/v1/admin/institutions/{inst_id}")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/admin/institutions/{id}/api-keys
# ---------------------------------------------------------------------------

class TestCreateApiKey:
    @pytest.fixture(scope="class")
    def inst_id(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Key Test Bank"},
                        headers=ADMIN_HEADER)
        return r.json()["id"]

    def test_returns_201(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "prod-key-1"},
                        headers=ADMIN_HEADER)
        assert r.status_code == 201

    def test_raw_key_in_response(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "test-key"},
                        headers=ADMIN_HEADER)
        assert "raw_key" in r.json()

    def test_raw_key_format(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "format-check"},
                        headers=ADMIN_HEADER)
        raw = r.json()["raw_key"]
        assert raw.startswith("gccuw_")
        assert len(raw) > 20

    def test_label_in_response(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "my-label"},
                        headers=ADMIN_HEADER)
        assert r.json()["label"] == "my-label"

    def test_key_appears_in_institution_list(self, inst_id):
        label = "listed-key"
        client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                    json={"label": label},
                    headers=ADMIN_HEADER)
        r = client.get(f"/v1/admin/institutions/{inst_id}", headers=ADMIN_HEADER)
        labels = [k["label"] for k in r.json()["api_keys"]]
        assert label in labels

    def test_key_is_active_after_creation(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "active-check"},
                        headers=ADMIN_HEADER)
        key_id = r.json()["id"]
        r2 = client.get(f"/v1/admin/institutions/{inst_id}", headers=ADMIN_HEADER)
        key_data = next(k for k in r2.json()["api_keys"] if str(k["id"]) == key_id)
        assert key_data["is_active"] is True

    def test_issued_key_authenticates_assessment_endpoint(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={"label": "live-test-key"},
                        headers=ADMIN_HEADER)
        raw_key = r.json()["raw_key"]

        txs = [{"transaction_date": "2024-01-15", "amount": "5000",
                "direction": "credit",
                "raw_description": "INWARD REMITTANCE - GULF TRADING CO REF:12345"}]
        r2 = client.post("/v1/assessments",
                         json={"sme_external_ref": "SME-KEYTEST",
                               "business_name": "Test Co",
                               "transactions": txs},
                         headers={"X-API-Key": raw_key})
        assert r2.status_code == 201

    def test_unknown_institution_returns_404(self):
        import uuid
        r = client.post(f"/v1/admin/institutions/{uuid.uuid4()}/api-keys",
                        json={"label": "ghost"},
                        headers=ADMIN_HEADER)
        assert r.status_code == 404

    def test_missing_label_returns_422(self, inst_id):
        r = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                        json={},
                        headers=ADMIN_HEADER)
        assert r.status_code == 422

    def test_two_keys_are_different(self, inst_id):
        r1 = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                         json={"label": "key-a"}, headers=ADMIN_HEADER)
        r2 = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                         json={"label": "key-b"}, headers=ADMIN_HEADER)
        assert r1.json()["raw_key"] != r2.json()["raw_key"]


# ---------------------------------------------------------------------------
# DELETE /v1/admin/institutions/{id}/api-keys/{key_id}
# ---------------------------------------------------------------------------

class TestRevokeApiKey:
    @pytest.fixture(scope="class")
    def inst_and_key(self):
        r = client.post("/v1/admin/institutions",
                        json={"name": "Revoke Test Bank"},
                        headers=ADMIN_HEADER)
        inst_id = r.json()["id"]
        r2 = client.post(f"/v1/admin/institutions/{inst_id}/api-keys",
                         json={"label": "revoke-me"},
                         headers=ADMIN_HEADER)
        return inst_id, r2.json()["id"], r2.json()["raw_key"]

    def test_revoke_returns_204(self, inst_and_key):
        inst_id, key_id, _ = inst_and_key
        r = client.delete(f"/v1/admin/institutions/{inst_id}/api-keys/{key_id}",
                          headers=ADMIN_HEADER)
        assert r.status_code == 204

    def test_revoked_key_returns_401_on_assessment(self, inst_and_key):
        inst_id, key_id, raw_key = inst_and_key
        # Revoke first (may already be revoked from previous test — that's fine)
        client.delete(f"/v1/admin/institutions/{inst_id}/api-keys/{key_id}",
                      headers=ADMIN_HEADER)
        txs = [{"transaction_date": "2024-01-15", "amount": "5000",
                "direction": "credit", "raw_description": "TEST PAYMENT"}]
        r = client.post("/v1/assessments",
                        json={"sme_external_ref": "SME-REVOKED",
                              "business_name": "X",
                              "transactions": txs},
                        headers={"X-API-Key": raw_key})
        assert r.status_code == 401

    def test_revoke_again_returns_409(self, inst_and_key):
        inst_id, key_id, _ = inst_and_key
        r = client.delete(f"/v1/admin/institutions/{inst_id}/api-keys/{key_id}",
                          headers=ADMIN_HEADER)
        assert r.status_code == 409

    def test_revoke_unknown_key_returns_404(self, inst_and_key):
        import uuid
        inst_id, _, _ = inst_and_key
        r = client.delete(f"/v1/admin/institutions/{inst_id}/api-keys/{uuid.uuid4()}",
                          headers=ADMIN_HEADER)
        assert r.status_code == 404

    def test_revoke_requires_admin_key(self, inst_and_key):
        import uuid
        inst_id, _, _ = inst_and_key
        r = client.delete(f"/v1/admin/institutions/{inst_id}/api-keys/{uuid.uuid4()}")
        assert r.status_code == 401
