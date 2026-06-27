from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, Enum as SAEnum, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
import enum
from app.db.base import Base


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FlagSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AssessmentReport(Base):
    __tablename__ = "assessment_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), unique=True, nullable=False)

    # Core financial metrics
    dscr = Column(Numeric(10, 4), nullable=True)
    avg_monthly_revenue = Column(Numeric(18, 3), nullable=True)
    avg_monthly_expenses = Column(Numeric(18, 3), nullable=True)
    avg_monthly_net = Column(Numeric(18, 3), nullable=True)

    # Risk metrics
    burn_rate_monthly = Column(Numeric(18, 3), nullable=True)
    runway_months = Column(Numeric(6, 1), nullable=True)
    revenue_volatility_index = Column(Numeric(8, 4), nullable=True)
    risk_score = Column(Integer, nullable=True)
    risk_level = Column(SAEnum(RiskLevel), nullable=True)

    # Full report payload for API response
    report_json = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    assessment = relationship("Assessment", back_populates="report")


class AnomalyFlag(Base):
    __tablename__ = "anomaly_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    flag_type = Column(String(100), nullable=False)
    description = Column(String(500), nullable=False)
    severity = Column(SAEnum(FlagSeverity), nullable=False)
    detected_at = Column(DateTime(timezone=True), server_default=func.now())

    assessment = relationship("Assessment", back_populates="flags")
