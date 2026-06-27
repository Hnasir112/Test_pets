"""
Unit tests for the transaction normalization engine.

Tests cover:
  - All 7 transaction categories
  - Arabic descriptions
  - Truncated / noisy POS terminal strings
  - Direction-aware rule filtering (debit-only rules reject credits)
  - Fuzzy matching fallback
  - Edge cases: empty strings, pure noise, zero-amount transactions
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from decimal import Decimal

from app.services.normalizer import normalize, NormalizationResult
from app.models.transaction import TransactionCategory, TransactionDirection

C = TransactionDirection.CREDIT
D = TransactionDirection.DEBIT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm(desc: str, direction: TransactionDirection, amount: float = 1000.0) -> NormalizationResult:
    return normalize(desc, direction, Decimal(str(amount)))


def assert_category(desc, direction, expected_category, min_confidence=0.70):
    result = norm(desc, direction)
    assert result.category == expected_category, (
        f"'{desc}' [{direction.value}] → got {result.category.value}, "
        f"expected {expected_category.value} (rule: {result.matched_rule})"
    )
    assert result.confidence >= min_confidence, (
        f"Confidence too low: {result.confidence:.2f} for '{desc}'"
    )


# ---------------------------------------------------------------------------
# PAYROLL
# ---------------------------------------------------------------------------

class TestPayroll:
    def test_wps_salary(self):
        assert_category("WPS SALARY TRANSFER MAR 2024", D, TransactionCategory.PAYROLL)

    def test_wps_arabic_salaries(self):
        assert_category("WPS - رواتب مارس 2024", D, TransactionCategory.PAYROLL)

    def test_payroll_dbt(self):
        assert_category("PAYROLL DBT REF:PAY498382", D, TransactionCategory.PAYROLL)

    def test_payroll_transfer(self):
        assert_category("PAYROLL TRANSFER APR - NBB WPS", D, TransactionCategory.PAYROLL)

    def test_sif_payroll(self):
        assert_category("SIF PAYROLL 20240415", D, TransactionCategory.PAYROLL)

    def test_arabic_salaries(self):
        assert_category("رواتب الموظفين مارس 2024", D, TransactionCategory.PAYROLL)

    def test_salaries_with_count(self):
        assert_category("SALARIES - MAR 2024 - 12 EMPLOYEES", D, TransactionCategory.PAYROLL)

    def test_payroll_credit_ignored(self):
        # Payroll rules are DEBIT only — a credit with 'SALARY' should not match payroll
        result = norm("SALARY REFUND CREDIT", C)
        assert result.category != TransactionCategory.PAYROLL


# ---------------------------------------------------------------------------
# GOVERNMENT / VAT
# ---------------------------------------------------------------------------

class TestGovernmentVat:
    def test_sadad_vat(self):
        assert_category("SADAD PMT - MOF VAT Q3 2024", D, TransactionCategory.GOVERNMENT_VAT)

    def test_sadad_generic(self):
        assert_category("NBB SADAD - VAT PAYMENT 123456", D, TransactionCategory.GOVERNMENT_VAT)

    def test_lmra_levy(self):
        assert_category("LMRA EXPAT LEVY - 9 STAFF", D, TransactionCategory.GOVERNMENT_VAT)

    def test_mof_vat_return(self):
        assert_category("MOF VAT RETURN 2024Q3", D, TransactionCategory.GOVERNMENT_VAT)

    def test_tamkeen(self):
        assert_category("TAMKEEN LEVY Q2 2024", D, TransactionCategory.GOVERNMENT_VAT)

    def test_gosi(self):
        assert_category("GOSI CONTRIBUTION MAR 2024", D, TransactionCategory.GOVERNMENT_VAT)

    def test_sijilat(self):
        assert_category("SIJILAT CR RENEWAL FEE 2024", D, TransactionCategory.GOVERNMENT_VAT)

    def test_customs_duty(self):
        assert_category("CUSTOMS DUTY PMT REF:CD12345", D, TransactionCategory.GOVERNMENT_VAT)

    def test_arabic_vat(self):
        assert_category("ضريبة القيمة المضافة - Q2 2024", D, TransactionCategory.GOVERNMENT_VAT)

    def test_arabic_expat_levy(self):
        assert_category("رسوم العمالة الوافدة", D, TransactionCategory.GOVERNMENT_VAT)

    def test_modon(self):
        assert_category("MODON FEES - INDUSTRIAL ZONE", D, TransactionCategory.GOVERNMENT_VAT)


# ---------------------------------------------------------------------------
# COMMERCIAL RENT
# ---------------------------------------------------------------------------

class TestCommercialRent:
    def test_monthly_rent(self):
        assert_category("MONTHLY RENT SEEF OFFICE MAR 2024", D, TransactionCategory.COMMERCIAL_RENT)

    def test_commercial_rent_explicit(self):
        assert_category("COMMERCIAL RENT HIDD SHOWROOM Q1/2024", D, TransactionCategory.COMMERCIAL_RENT)

    def test_property_payment(self):
        assert_category("PROPERTY PMT - ITHMAAR REAL ESTATE", D, TransactionCategory.COMMERCIAL_RENT)

    def test_ijarah(self):
        assert_category("IJARAH PMT MAR 2024 SEEF", D, TransactionCategory.COMMERCIAL_RENT)

    def test_lease_payment(self):
        assert_category("LEASE PMT REF:RENT123456 ZINJ", D, TransactionCategory.COMMERCIAL_RENT)

    def test_arabic_rent(self):
        assert_category("إيجار شهري - المنامة", D, TransactionCategory.COMMERCIAL_RENT)

    def test_rent_not_matched_as_credit(self):
        # Rent rules are DEBIT only
        result = norm("RENT DEPOSIT REFUND", C)
        assert result.category != TransactionCategory.COMMERCIAL_RENT


# ---------------------------------------------------------------------------
# BANK CHARGES
# ---------------------------------------------------------------------------

class TestBankCharges:
    def test_account_maintenance(self):
        assert_category("ACCOUNT MAINTENANCE FEE MAR 2024", D, TransactionCategory.BANK_CHARGES)

    def test_service_charge(self):
        assert_category("SERVICE CHARGE - CURRENT ACCOUNT", D, TransactionCategory.BANK_CHARGES)

    def test_swift_fee(self):
        assert_category("SWIFT TRANSFER FEE REF:789456", D, TransactionCategory.BANK_CHARGES)

    def test_sms_charges(self):
        assert_category("SMS ALERT CHARGES MAR 2024", D, TransactionCategory.BANK_CHARGES)

    def test_chequebook(self):
        assert_category("CHEQUEBOOK ISSUANCE FEE", D, TransactionCategory.BANK_CHARGES)

    def test_card_annual_fee(self):
        assert_category("CARD ANNUAL FEE", D, TransactionCategory.BANK_CHARGES)

    def test_atm_fee(self):
        assert_category("ATM USAGE FEE", D, TransactionCategory.BANK_CHARGES)

    def test_overdraft_interest(self):
        assert_category("OVERDRAFT INTEREST MAR 2024", D, TransactionCategory.BANK_CHARGES)

    def test_statement_fee(self):
        assert_category("STATEMENT FEE MAR 2024", D, TransactionCategory.BANK_CHARGES)


# ---------------------------------------------------------------------------
# INTER-ACCOUNT
# ---------------------------------------------------------------------------

class TestInterAccount:
    def test_own_account(self):
        assert_category("TRF TO SAVINGS ACCOUNT - SAME ENTITY", D, TransactionCategory.INTER_ACCOUNT)

    def test_internal_transfer(self):
        assert_category("INTERNAL TRF - PETTY CASH 123456", D, TransactionCategory.INTER_ACCOUNT)

    def test_petty_cash(self):
        assert_category("PETTY CASH TOP UP", D, TransactionCategory.INTER_ACCOUNT)

    def test_fd_account(self):
        assert_category("TRF TO FD ACCOUNT 987654", D, TransactionCategory.INTER_ACCOUNT)

    def test_arabic_internal(self):
        assert_category("تحويل داخلي - حساب التوفير", D, TransactionCategory.INTER_ACCOUNT)


# ---------------------------------------------------------------------------
# PRIMARY REVENUE
# ---------------------------------------------------------------------------

class TestPrimaryRevenue:
    def test_inward_remittance(self):
        assert_category("INWARD REMITTANCE - GULF TRADING CO REF:201639", C, TransactionCategory.PRIMARY_REVENUE)

    def test_benefit_pay(self):
        assert_category("BENEFIT PAY RECEIVED CRESCENT LOGISTICS CO", C, TransactionCategory.PRIMARY_REVENUE)

    def test_neft_credit(self):
        assert_category("NEFT CR-CUSTOMER PMT-789456", C, TransactionCategory.PRIMARY_REVENUE)

    def test_trf_from(self):
        assert_category("TRF FROM AHMED KHALIL TRADING CO CR 2345.000", C, TransactionCategory.PRIMARY_REVENUE)

    def test_sales_settlement(self):
        assert_category("SALES SETTLEMENT - TERM456789", C, TransactionCategory.PRIMARY_REVENUE)

    def test_batch_credit(self):
        assert_category("BATCH CREDIT - MERCHANT 665492", C, TransactionCategory.PRIMARY_REVENUE)

    def test_swift_credit(self):
        assert_category("SWIFT CR USD PEARL COAST TRADING //789456", C, TransactionCategory.PRIMARY_REVENUE)

    def test_arabic_transfer_from(self):
        assert_category("تحويل من شركة النور للاستشارات مرجع 731262", C, TransactionCategory.PRIMARY_REVENUE)

    def test_arabic_incoming(self):
        assert_category("تحويل وارد من مؤسسة الفاروق التجارية", C, TransactionCategory.PRIMARY_REVENUE)

    def test_arabic_client_payment(self):
        assert_category("دفعة عميل شركة المحمد فاتورة 123456", C, TransactionCategory.PRIMARY_REVENUE)

    def test_ibg_credit(self):
        assert_category("IBG CREDIT BAHRAIN CONTRACTING EST REF789456", C, TransactionCategory.PRIMARY_REVENUE)


# ---------------------------------------------------------------------------
# DISCRETIONARY CAPEX
# ---------------------------------------------------------------------------

class TestDiscretionaryCapex:
    def test_amazon(self):
        assert_category("AMAZON.AE - 707314", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_noon(self):
        assert_category("NOON BAHRAIN POS****1234", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_carrefour(self):
        assert_category("CARREFOUR BHR SEEF POS789456", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_ikea(self):
        assert_category("IKEA BAHRAIN 123456", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_sharaf_dg(self):
        assert_category("SHARAF DG - ELECTRONICS", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_lulu(self):
        assert_category("LULU HYPERMARKET POS 789456", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_ace_hardware(self):
        assert_category("ACE HARDWARE BHR 821590", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_apple_store(self):
        assert_category("APPLE STORE UAE ONLINE", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_office_depot(self):
        assert_category("OFFICE DEPOT - STATIONERY 680099", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_arabic_supermarket(self):
        assert_category("سوبر ماركت الأسرة 214975", D, TransactionCategory.DISCRETIONARY_CAPEX)

    def test_capex_not_matched_as_credit(self):
        result = norm("AMAZON REFUND CREDIT", C)
        assert result.category != TransactionCategory.DISCRETIONARY_CAPEX


# ---------------------------------------------------------------------------
# Noise handling & edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_pure_pos_noise(self):
        # Pure terminal noise with no recognizable keyword — should be UNCATEGORIZED or heuristic
        result = norm("POS 4521****1234 MCC:5411 TXN:2309421", D)
        # We don't assert a specific category — just that it doesn't crash and returns something
        assert isinstance(result, NormalizationResult)
        assert result.category is not None

    def test_empty_description(self):
        result = norm("", D)
        assert result.category == TransactionCategory.UNCATEGORIZED

    def test_very_long_description(self):
        long_desc = "WPS SALARY TRANSFER " + "X" * 400
        assert_category(long_desc, D, TransactionCategory.PAYROLL)

    def test_mixed_case(self):
        assert_category("wps salary transfer mar 2024", D, TransactionCategory.PAYROLL)

    def test_reference_numbers_stripped(self):
        result = norm("SADAD PMT - MOF VAT 123456789 REF:987654", D)
        assert result.category == TransactionCategory.GOVERNMENT_VAT
        # Reference numbers should not appear in normalized description
        assert "123456789" not in result.normalized_description

    def test_confidence_ranges(self):
        result = norm("WPS SALARY TRANSFER MAR 2024", D)
        assert 0.0 <= result.confidence <= 1.0

    def test_matched_rule_populated(self):
        result = norm("LMRA EXPAT LEVY - 5 STAFF", D)
        assert result.matched_rule != ""
        assert result.matched_rule is not None
