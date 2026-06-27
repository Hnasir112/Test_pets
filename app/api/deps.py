"""
FastAPI dependency: API key authentication.

Banks send their key in the `X-API-Key` header. The key is hashed with
SHA-256 and compared against the key_hash column in api_keys. If valid
and active, the associated Institution is returned.

Usage:
    @router.post("/assessments")
    def create(institution: Institution = Depends(get_institution)):
        ...
"""

import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.api_key import ApiKey
from app.models.institution import Institution


_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing API key",
    headers={"WWW-Authenticate": "ApiKey"},
)


def get_institution(
    raw_key: str | None = Security(_API_KEY_HEADER),
    db: Session = Depends(get_db),
) -> Institution:
    """
    Resolve an X-API-Key header to an Institution.

    Raises 401 if the header is absent, the key is not found, or the key
    has been deactivated.
    """
    if not raw_key:
        raise _UNAUTHORIZED

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    api_key: ApiKey | None = (
        db.query(ApiKey).filter_by(key_hash=key_hash, is_active=True).first()
    )
    if api_key is None:
        raise _UNAUTHORIZED

    # Record last usage (best-effort — not critical if this fails)
    api_key.last_used_at = datetime.now(timezone.utc)

    return api_key.institution
