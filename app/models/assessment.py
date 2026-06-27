from sqlalchemy import Column, String, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
import enum
from app.db.base import Base


class AssessmentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    institution_id = Column(UUID(as_uuid=True), ForeignKey("institutions.id"), nullable=False)
    sme_profile_id = Column(UUID(as_uuid=True), ForeignKey("sme_profiles.id"), nullable=False)
    status = Column(SAEnum(AssessmentStatus), default=AssessmentStatus.PENDING)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    institution = relationship("Institution", back_populates="assessments")
    sme_profile = relationship("SmeProfile", back_populates="assessments")
    transactions = relationship("Transaction", back_populates="assessment")
    report = relationship("AssessmentReport", back_populates="assessment", uselist=False)
    flags = relationship("AnomalyFlag", back_populates="assessment")
