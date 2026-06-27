from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
import enum
from app.db.base import Base


class TransactionDirection(str, enum.Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class TransactionCategory(str, enum.Enum):
    PRIMARY_REVENUE = "primary_revenue"
    PAYROLL = "payroll"
    GOVERNMENT_VAT = "government_vat"
    COMMERCIAL_RENT = "commercial_rent"
    DISCRETIONARY_CAPEX = "discretionary_capex"
    BANK_CHARGES = "bank_charges"
    INTER_ACCOUNT = "inter_account"
    UNCATEGORIZED = "uncategorized"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    transaction_date = Column(DateTime(timezone=True), nullable=False)
    raw_description = Column(String(500), nullable=False)
    normalized_description = Column(String(500), nullable=True)
    amount = Column(Numeric(18, 3), nullable=False)
    direction = Column(SAEnum(TransactionDirection), nullable=False)
    category = Column(SAEnum(TransactionCategory), default=TransactionCategory.UNCATEGORIZED)
    currency = Column(String(3), default="BHD")
    raw_json = Column(JSONB, nullable=True)

    assessment = relationship("Assessment", back_populates="transactions")
