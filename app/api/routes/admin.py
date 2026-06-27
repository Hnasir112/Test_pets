"""
Admin endpoints — institution onboarding and API key management.

Protected by the master X-Admin-Key header (must match API_SECRET_KEY in .env).
These endpoints are called by you (the operator), not by banks.

POST /v1/admin/institutions                    — register a new bank
GET  /v1/admin/institutions/{id}               — view institution + key summary
POST /v1/admin/institutions/{id}/api-keys      — issue a new API key
DELETE /v1/admin/institutions/{id}/api-keys/{key_id} — revoke a key
"""

import hashlib
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.models.api_key import ApiKey
from app.models.institution import Institution, InstitutionTier

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_ADMIN_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)


# ---------------------------------------------------------------------------
# Admin auth dependency
# ---------------------------------------------------------------------------

def require_admin(raw_key: str | None = Security(_ADMIN_HEADER)) -> None:
    if not raw_key or raw_key != settings.API_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key",
            headers={"WWW-Authenticate": "AdminKey"},
        )


# ---------------------------------------------------------------------------
# Schemas (admin-specific, kept local)
# ---------------------------------------------------------------------------

class InstitutionCreate(BaseModel):
    name: str = Field(..., max_length=255)
    tier: InstitutionTier = InstitutionTier.PILOT
    monthly_assessment_cap: str = Field(default="100", max_length=10)


class ApiKeyCreate(BaseModel):
    label: str = Field(..., max_length=100, description="Human-readable label, e.g. 'production-key-1'")


class ApiKeyOut(BaseModel):
    id: UUID
    label: str
    is_active: bool
    created_at: str
    last_used_at: str | None


class ApiKeyCreated(BaseModel):
    id: UUID
    label: str
    raw_key: str   # shown ONCE — not stored, cannot be recovered
    created_at: str


class InstitutionOut(BaseModel):
    id: UUID
    name: str
    tier: str
    monthly_assessment_cap: str
    is_active: bool
    created_at: str
    api_keys: list[ApiKeyOut]


# ---------------------------------------------------------------------------
# POST /v1/admin/institutions
# ---------------------------------------------------------------------------

@router.post(
    "/institutions",
    response_model=InstitutionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new bank institution",
    dependencies=[Depends(require_admin)],
)
def create_institution(
    body: InstitutionCreate,
    db: Session = Depends(get_db),
) -> InstitutionOut:
    """
    Onboard a new bank as a client. Returns the institution record.
    Issue API keys separately with the next endpoint.
    """
    inst = Institution(
        name=body.name,
        tier=body.tier,
        monthly_assessment_cap=body.monthly_assessment_cap,
        is_active=True,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return _institution_out(inst, [])


# ---------------------------------------------------------------------------
# GET /v1/admin/institutions/{id}
# ---------------------------------------------------------------------------

@router.get(
    "/institutions/{institution_id}",
    response_model=InstitutionOut,
    summary="View institution details and API keys",
    dependencies=[Depends(require_admin)],
)
def get_institution(
    institution_id: UUID,
    db: Session = Depends(get_db),
) -> InstitutionOut:
    inst = _get_or_404(db, institution_id)
    keys = db.query(ApiKey).filter_by(institution_id=institution_id).all()
    return _institution_out(inst, keys)


# ---------------------------------------------------------------------------
# POST /v1/admin/institutions/{id}/api-keys
# ---------------------------------------------------------------------------

@router.post(
    "/institutions/{institution_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new API key for an institution",
    dependencies=[Depends(require_admin)],
)
def create_api_key(
    institution_id: UUID,
    body: ApiKeyCreate,
    db: Session = Depends(get_db),
) -> ApiKeyCreated:
    """
    Generate and return a new API key for a bank institution.

    **The raw key is shown exactly once** — store it securely.
    Only a SHA-256 hash is persisted in the database.

    The bank uses this key in the `X-API-Key` header on every request.
    """
    _get_or_404(db, institution_id)

    raw_key = f"gccuw_{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    api_key = ApiKey(
        institution_id=institution_id,
        key_hash=key_hash,
        label=body.label,
        is_active=True,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return ApiKeyCreated(
        id=api_key.id,
        label=api_key.label,
        raw_key=raw_key,
        created_at=api_key.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# DELETE /v1/admin/institutions/{id}/api-keys/{key_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/institutions/{institution_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    dependencies=[Depends(require_admin)],
)
def revoke_api_key(
    institution_id: UUID,
    key_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    """
    Deactivate an API key. Requests using this key will immediately
    receive 401. The key record is retained for audit purposes.
    """
    _get_or_404(db, institution_id)

    key = db.get(ApiKey, key_id)
    if key is None or key.institution_id != institution_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    if not key.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Key is already inactive")

    key.is_active = False
    db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(db: Session, institution_id: UUID) -> Institution:
    inst = db.get(Institution, institution_id)
    if inst is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Institution not found")
    return inst


def _institution_out(inst: Institution, keys: list[ApiKey]) -> InstitutionOut:
    return InstitutionOut(
        id=inst.id,
        name=inst.name,
        tier=inst.tier.value,
        monthly_assessment_cap=inst.monthly_assessment_cap,
        is_active=inst.is_active,
        created_at=inst.created_at.isoformat() if inst.created_at else "",
        api_keys=[
            ApiKeyOut(
                id=k.id,
                label=k.label,
                is_active=k.is_active,
                created_at=k.created_at.isoformat() if k.created_at else "",
                last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            )
            for k in keys
        ],
    )
