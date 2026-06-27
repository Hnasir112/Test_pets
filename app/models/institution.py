from sqlalchemy import Column, String, Boolean, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
import enum
from app.db.base import Base


class InstitutionTier(str, enum.Enum):
    PILOT = "pilot"
    STANDARD = "standard"
    ENTERPRISE = "enterprise"


class Institution(Base):
    __tablename__ = "institutions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    tier = Column(SAEnum(InstitutionTier), default=InstitutionTier.PILOT, nullable=False)
    monthly_assessment_cap = Column(String(10), default="100")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    api_keys = relationship("ApiKey", back_populates="institution")
    sme_profiles = relationship("SmeProfile", back_populates="institution")
    assessments = relationship("Assessment", back_populates="institution")
