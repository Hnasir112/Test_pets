"""
Synthetic GCC bank transaction data generator.

Produces realistic, messy, bilingual (Arabic/English) transaction histories
that mirror actual Bahrain bank statement export formats. Used exclusively
for development and stress-testing — no real customer data is ever used.

Scenarios:
  HEALTHY   — stable revenue, positive DSCR, clean patterns
  STRESSED  — declining revenue, negative trend, high burn rate
  ANOMALOUS — round-trip transactions, structured cash behaviour
"""

import sys
import os
import random
import uuid
import json
import csv
import hashlib
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.base import SessionLocal
from app.models import (
    Institution, InstitutionTier, ApiKey, SmeProfile,
    Assessment, AssessmentStatus, Transaction,
    TransactionDirection, TransactionCategory,
)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()

random.seed(42)


# ---------------------------------------------------------------------------
# GCC-realistic transaction description templates
# These mirror actual Bahrain/GCC bank statement text fields — intentionally
# messy, truncated, and mixed-language to simulate real export noise.
# ---------------------------------------------------------------------------

REVENUE_DESCRIPTIONS = [
    # English wire/transfer formats
    "TRF FROM {company} CR {ref}",
    "INWARD REMITTANCE - {company} REF:{ref}",
    "ONLINE TRF CR - INV-{ref} {company}",
    "BENEFIT PAY RECEIVED {company}",
    "NEFT CR-CUSTOMER PMT-{ref}",
    "IBG CREDIT {company} REF{ref}",
    "SWIFT CR USD {company} //{ref}",
    "POS CREDIT VISA****{card} {company}",
    # Arabic transliterated / mixed
    "تحويل من {company_ar} مرجع {ref}",
    "دفعة عميل {company_ar} فاتورة {ref}",
    "ايراد {company_ar}",
    "تحويل وارد من {company_ar}",
    # Truncated POS noise
    "POS {card}**** MCC:5411 TXN:{ref}",
    "SALES SETTLEMENT - TERM{ref}",
    "BATCH CREDIT - MERCHANT {ref}",
]

PAYROLL_DESCRIPTIONS = [
    "WPS SALARY TRANSFER {month} {year}",
    "PAYROLL DBT REF:PAY{ref}",
    "SALARIES - {month} {year} - {count} EMPLOYEES",
    "رواتب الموظفين {month_ar} {year}",
    "WPS - رواتب {month_ar}",
    "PAYROLL TRANSFER {month} - NBB WPS",
    "SIF PAYROLL {ref}",
    "SALARY PMT {count} STAFF {month}{year}",
]

RENT_DESCRIPTIONS = [
    "MONTHLY RENT {area} OFFICE {month} {year}",
    "PROPERTY PMT - {landlord}",
    "RENT - {area} SHOWROOM",
    "إيجار شهري - {area_ar}",
    "COMMERCIAL RENT {area} Q{quarter}/{year}",
    "LEASE PMT REF:RENT{ref} {area}",
    "ITHMAAR REAL ESTATE - {area} RENT",
    "IJARAH PMT {month} {year} {area}",
]

GOVERNMENT_DESCRIPTIONS = [
    "SADAD PMT - MOF VAT {quarter} {year}",
    "LMRA EXPAT LEVY - {count} STAFF",
    "SIJILAT CR RENEWAL FEE {year}",
    "MOF VAT RETURN {year}Q{quarter}",
    "TAMKEEN LEVY Q{quarter} {year}",
    "GOSI CONTRIBUTION {month} {year}",
    "NBB SADAD - VAT PAYMENT {ref}",
    "CUSTOMS DUTY PMT REF:{ref}",
    "رسوم العمالة الوافدة",
    "ضريبة القيمة المضافة - {quarter} {year}",
    "MODON FEES - INDUSTRIAL ZONE",
    "CRPD REGISTRATION FEE {year}",
]

CAPEX_DESCRIPTIONS = [
    "AMAZON.AE - {ref}",
    "NOON BAHRAIN POS****{card}",
    "CARREFOUR BHR {area} POS{ref}",
    "APPLE STORE UAE ONLINE",
    "OFFICE DEPOT - STATIONERY {ref}",
    "IKEA BAHRAIN {ref}",
    "VIRGIN MEGASTORE {ref}",
    "GEANT BHR POS {card}****",
    "SHARAF DG - ELECTRONICS",
    "LULU HYPERMARKET POS {ref}",
    "ACE HARDWARE BHR {ref}",
    "TECH SUPPLIES MANAMA {ref}",
    "AL OSRA SUPERMARKET",
    "سوبر ماركت الأسرة {ref}",
    "ZOOM STATIONERY {ref}",
]

BANK_CHARGE_DESCRIPTIONS = [
    "ACCOUNT MAINTENANCE FEE {month} {year}",
    "SERVICE CHARGE - CURRENT ACCOUNT",
    "SWIFT TRANSFER FEE REF:{ref}",
    "SMS ALERT CHARGES {month} {year}",
    "CHEQUEBOOK ISSUANCE FEE",
    "INTERNATIONAL TRANSFER CHARGE",
    "OVERDRAFT INTEREST {month} {year}",
    "ATM USAGE FEE",
    "CARD ANNUAL FEE",
    "STATEMENT FEE {month} {year}",
]

INTER_ACCOUNT_DESCRIPTIONS = [
    "TRF TO SAVINGS ACCOUNT - SAME ENTITY",
    "INTERNAL TRF - PETTY CASH {ref}",
    "OWN ACCOUNT TRANSFER {ref}",
    "TRF TO FD ACCOUNT {ref}",
    "تحويل داخلي - حساب التوفير",
]

NOISE_DESCRIPTIONS = [
    "AUTO PMT REF:AP{ref}",
    "POS {card}****{ref2} MCC:{mcc}",
    "RTGS{ref} /BNF/{company}/",
    "ACH DEBIT {ref} CCD",
    "MISC CR {ref}",
    "ADJUSTMENT CR/{ref}",
]

# Arabic company name fragments
ARABIC_COMPANIES = [
    "شركة المحمد للتجارة",
    "مؤسسة الخليج",
    "شركة البحرين للمقاولات",
    "مجموعة الأنصاري",
    "شركة دلمون للتوريدات",
    "مؤسسة الفاروق التجارية",
    "شركة الوفاء للخدمات",
    "مجموعة البنيان العقارية",
    "شركة النور للاستشارات",
    "مؤسسة الرافدين",
]

ENGLISH_COMPANIES = [
    "GULF TRADING CO WLL",
    "BAHRAIN CONTRACTING EST",
    "AL MANAMA SUPPLIES",
    "KHALEEJI SERVICES LLC",
    "DILMUN TRADING CO",
    "NATIONAL CONSULTING GRP",
    "SEEF PROPERTIES WLL",
    "PEARL COAST TRADING",
    "ARABIAN GULF SOLUTIONS",
    "CRESCENT LOGISTICS CO",
    "FINESTEEL BAHRAIN WLL",
    "TAMEER CONSTRUCTION CO",
]

BAHRAIN_AREAS = [
    "SEEF", "MANAMA", "JUFFAIR", "SALMABAD", "HAMALA",
    "BUDAIYA", "RIFFA", "MUHARRAQ", "HIDD", "ZINJ",
]

ARABIC_AREAS = ["السيف", "المنامة", "الجفير", "سلماباد", "حمالة"]

ARABIC_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل",
    5: "مايو", 6: "يونيو", 7: "يوليو", 8: "أغسطس",
    9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}

ENGLISH_MONTHS = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

class Scenario(str, Enum):
    HEALTHY = "healthy"
    STRESSED = "stressed"
    ANOMALOUS = "anomalous"


SCENARIO_CONFIGS = {
    Scenario.HEALTHY: {
        "monthly_revenue_base": (8000, 18000),
        "revenue_variance": 0.15,
        "revenue_trend": 0.02,
        "monthly_payroll": (3000, 6000),
        "monthly_rent": (800, 2000),
        "monthly_capex_count": (2, 6),
        "monthly_capex_amount": (50, 800),
        "include_anomaly": False,
    },
    Scenario.STRESSED: {
        "monthly_revenue_base": (5000, 10000),
        "revenue_variance": 0.35,
        "revenue_trend": -0.06,
        "monthly_payroll": (4000, 7000),
        "monthly_rent": (1500, 3000),
        "monthly_capex_count": (3, 8),
        "monthly_capex_amount": (100, 1500),
        "include_anomaly": False,
    },
    Scenario.ANOMALOUS: {
        "monthly_revenue_base": (10000, 20000),
        "revenue_variance": 0.1,
        "revenue_trend": 0.01,
        "monthly_payroll": (2000, 4000),
        "monthly_rent": (1000, 2000),
        "monthly_capex_count": (1, 3),
        "monthly_capex_amount": (50, 300),
        "include_anomaly": True,
    },
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def rand_ref():
    return str(random.randint(100000, 999999))


def rand_card():
    return str(random.randint(1000, 9999))


def rand_mcc():
    return random.choice(["5411", "5812", "7389", "5045", "5065", "4814"])


def fill_template(template: str, month: int, year: int, quarter: int, **kwargs) -> str:
    replacements = {
        "ref": rand_ref(),
        "ref2": rand_ref(),
        "card": rand_card(),
        "mcc": rand_mcc(),
        "month": ENGLISH_MONTHS[month],
        "month_ar": ARABIC_MONTHS[month],
        "year": str(year),
        "quarter": str(quarter),
        "company": random.choice(ENGLISH_COMPANIES),
        "company_ar": random.choice(ARABIC_COMPANIES),
        "area": random.choice(BAHRAIN_AREAS),
        "area_ar": random.choice(ARABIC_AREAS),
        "landlord": random.choice(["ITHMAAR RE", "SEEF PROPS", "BAHRAIN HOLDINGS"]),
        "count": str(random.randint(3, 25)),
    }
    replacements.update(kwargs)
    try:
        return template.format(**replacements)
    except KeyError:
        return template


def random_business_day(year: int, month: int) -> datetime:
    """Return a random weekday within the given month (Bahrain: Sun-Thu workweek)."""
    import calendar
    _, days_in_month = calendar.monthrange(year, month)
    day = random.randint(1, days_in_month)
    dt = datetime(year, month, day)
    # Shift Friday/Saturday to Thursday (Bahrain weekend)
    if dt.weekday() == 4:  # Friday
        day = max(1, day - 1)
    elif dt.weekday() == 5:  # Saturday
        day = max(1, day - 2)
    return datetime(year, month, day)


# ---------------------------------------------------------------------------
# Core transaction generators per category
# ---------------------------------------------------------------------------

def gen_revenue_transactions(month: int, year: int, base_amount: float, variance: float) -> list[dict]:
    txns = []
    count = random.randint(2, 7)
    total = base_amount * random.uniform(1 - variance, 1 + variance)
    # Split total revenue across multiple incoming payments
    amounts = [random.random() for _ in range(count)]
    amounts = [a / sum(amounts) * total for a in amounts]

    for amount in amounts:
        txns.append({
            "date": random_business_day(year, month),
            "description": fill_template(random.choice(REVENUE_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
            "amount": round(amount, 3),
            "direction": TransactionDirection.CREDIT,
            "category": TransactionCategory.PRIMARY_REVENUE,
        })
    return txns


def gen_payroll(month: int, year: int, amount: float) -> dict:
    # Payroll hits on the last working day of the month, consistently
    day = random.randint(25, 28)
    try:
        dt = datetime(year, month, day)
    except ValueError:
        dt = datetime(year, month, 25)
    return {
        "date": dt,
        "description": fill_template(random.choice(PAYROLL_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
        "amount": round(amount * random.uniform(0.97, 1.03), 3),
        "direction": TransactionDirection.DEBIT,
        "category": TransactionCategory.PAYROLL,
    }


def gen_rent(month: int, year: int, amount: float) -> dict:
    # Rent hits on 1st-5th of the month
    day = random.randint(1, 5)
    return {
        "date": datetime(year, month, day),
        "description": fill_template(random.choice(RENT_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
        "amount": round(amount, 3),
        "direction": TransactionDirection.DEBIT,
        "category": TransactionCategory.COMMERCIAL_RENT,
    }


def gen_government_payments(month: int, year: int, payroll: float) -> list[dict]:
    txns = []
    quarter = (month - 1) // 3 + 1

    # VAT payment — quarterly (months 3, 6, 9, 12 of the year)
    if month in (3, 6, 9, 12):
        vat_amount = round(random.uniform(800, 3500), 3)
        txns.append({
            "date": random_business_day(year, month),
            "description": fill_template(random.choice(GOVERNMENT_DESCRIPTIONS[:5]), month, year, quarter),
            "amount": vat_amount,
            "direction": TransactionDirection.DEBIT,
            "category": TransactionCategory.GOVERNMENT_VAT,
        })

    # LMRA expat levy — monthly
    lmra = round(payroll * random.uniform(0.03, 0.08), 3)
    txns.append({
        "date": random_business_day(year, month),
        "description": fill_template("LMRA EXPAT LEVY - {count} STAFF", month, year, quarter),
        "amount": lmra,
        "direction": TransactionDirection.DEBIT,
        "category": TransactionCategory.GOVERNMENT_VAT,
    })

    return txns


def gen_capex(month: int, year: int, count: int, amount_range: tuple) -> list[dict]:
    txns = []
    for _ in range(count):
        txns.append({
            "date": random_business_day(year, month),
            "description": fill_template(random.choice(CAPEX_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
            "amount": round(random.uniform(*amount_range), 3),
            "direction": TransactionDirection.DEBIT,
            "category": TransactionCategory.DISCRETIONARY_CAPEX,
        })
    return txns


def gen_bank_charges(month: int, year: int) -> list[dict]:
    txns = []
    # Monthly maintenance fee — always present
    txns.append({
        "date": datetime(year, month, random.randint(1, 5)),
        "description": fill_template(random.choice(BANK_CHARGE_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
        "amount": round(random.uniform(5, 35), 3),
        "direction": TransactionDirection.DEBIT,
        "category": TransactionCategory.BANK_CHARGES,
    })
    # Occasional extra charge
    if random.random() < 0.3:
        txns.append({
            "date": random_business_day(year, month),
            "description": fill_template(random.choice(BANK_CHARGE_DESCRIPTIONS), month, year, (month - 1) // 3 + 1),
            "amount": round(random.uniform(2, 15), 3),
            "direction": TransactionDirection.DEBIT,
            "category": TransactionCategory.BANK_CHARGES,
        })
    return txns


def gen_round_trip_anomaly(month: int, year: int, amount: float) -> list[dict]:
    """
    Simulates a circular invoice / round-trip pattern:
    large amount transferred out then back in within days.
    This is a red flag for credit manipulation.
    """
    day_out = random.randint(5, 15)
    day_in = day_out + random.randint(1, 4)
    company = random.choice(ENGLISH_COMPANIES)
    return [
        {
            "date": datetime(year, month, day_out),
            "description": f"TRF TO {company} REF:{rand_ref()}",
            "amount": round(amount, 3),
            "direction": TransactionDirection.DEBIT,
            "category": TransactionCategory.INTER_ACCOUNT,
        },
        {
            "date": datetime(year, month, min(day_in, 28)),
            "description": f"TRF FROM {company} REF:{rand_ref()}",
            "amount": round(amount * random.uniform(0.98, 1.0), 3),
            "direction": TransactionDirection.CREDIT,
            "category": TransactionCategory.PRIMARY_REVENUE,
        },
    ]


# ---------------------------------------------------------------------------
# Main month generator
# ---------------------------------------------------------------------------

def generate_month(
    month: int,
    year: int,
    config: dict,
    revenue_multiplier: float,
    include_anomaly: bool,
) -> list[dict]:
    base_lo, base_hi = config["monthly_revenue_base"]
    base_revenue = random.uniform(base_lo, base_hi) * revenue_multiplier

    payroll_lo, payroll_hi = config["monthly_payroll"]
    payroll = random.uniform(payroll_lo, payroll_hi)

    rent_lo, rent_hi = config["monthly_rent"]
    rent = random.uniform(rent_lo, rent_hi)

    capex_count = random.randint(*config["monthly_capex_count"])

    txns = []
    txns += gen_revenue_transactions(month, year, base_revenue, config["revenue_variance"])
    txns.append(gen_payroll(month, year, payroll))
    txns.append(gen_rent(month, year, rent))
    txns += gen_government_payments(month, year, payroll)
    txns += gen_capex(month, year, capex_count, config["monthly_capex_amount"])
    txns += gen_bank_charges(month, year)

    if include_anomaly and random.random() < 0.4:
        anomaly_amount = round(random.uniform(5000, 15000), 3)
        txns += gen_round_trip_anomaly(month, year, anomaly_amount)

    return sorted(txns, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Full SME dataset generator
# ---------------------------------------------------------------------------

def generate_sme_dataset(
    scenario: Scenario,
    months: int = 12,
    end_year: int = 2024,
    end_month: int = 12,
) -> list[dict]:
    config = SCENARIO_CONFIGS[scenario]
    trend = config["revenue_trend"]

    all_transactions = []
    # Walk backwards from end_month/end_year
    for i in range(months - 1, -1, -1):
        m = end_month - i
        y = end_year
        while m <= 0:
            m += 12
            y -= 1
        revenue_multiplier = (1 + trend) ** (months - 1 - i)
        month_txns = generate_month(m, y, config, revenue_multiplier, config["include_anomaly"])
        all_transactions.extend(month_txns)

    return sorted(all_transactions, key=lambda x: x["date"])


# ---------------------------------------------------------------------------
# Database seeder
# ---------------------------------------------------------------------------

def seed_database(num_smes: int = 5) -> None:
    db = SessionLocal()
    try:
        # Create a demo institution
        institution = Institution(
            name="National Bank of Bahrain (Demo)",
            tier=InstitutionTier.PILOT,
            monthly_assessment_cap="500",
        )
        db.add(institution)
        db.flush()

        # Create an API key for the institution
        raw_key = f"gccuw_{secrets.token_hex(24)}"
        api_key = ApiKey(
            institution_id=institution.id,
            key_hash=hash_api_key(raw_key),
            label="Development Key",
        )
        db.add(api_key)

        print(f"\n  Institution: {institution.name}")
        print(f"  API Key (save this — shown once): {raw_key}\n")

        scenarios = [
            (Scenario.HEALTHY, "Al Manama Trading Co WLL"),
            (Scenario.HEALTHY, "Gulf Solutions Est"),
            (Scenario.STRESSED, "Dilmun Retail LLC"),
            (Scenario.ANOMALOUS, "Pearl Coast Ventures WLL"),
            (Scenario.STRESSED, "Seef Logistics Co"),
        ][:num_smes]

        for scenario, business_name in scenarios:
            profile = SmeProfile(
                institution_id=institution.id,
                external_ref=f"EXT-{rand_ref()}",
                business_name=business_name,
                registration_number=f"CR-{random.randint(10000, 99999)}-{random.randint(1, 9)}",
                industry=random.choice(["Retail", "Logistics", "Consulting", "Construction", "Trading"]),
            )
            db.add(profile)
            db.flush()

            assessment = Assessment(
                institution_id=institution.id,
                sme_profile_id=profile.id,
                status=AssessmentStatus.PENDING,
            )
            db.add(assessment)
            db.flush()

            txns = generate_sme_dataset(scenario)
            for t in txns:
                db.add(Transaction(
                    assessment_id=assessment.id,
                    transaction_date=t["date"],
                    raw_description=t["description"],
                    amount=Decimal(str(t["amount"])),
                    direction=t["direction"],
                    category=t["category"],
                    currency="BHD",
                    raw_json={"raw": t["description"], "source": "synthetic"},
                ))

            print(f"  [{scenario.value.upper():10s}] {business_name} — {len(txns)} transactions")

        db.commit()
        print(f"\n  Seeded {num_smes} SME profiles into the database.\n")

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CSV export (for manual inspection)
# ---------------------------------------------------------------------------

def export_csv(scenario: Scenario, output_path: str = None) -> None:
    txns = generate_sme_dataset(scenario, months=12)
    path = output_path or f"/tmp/gcc_synthetic_{scenario.value}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "description", "amount", "direction", "category"])
        writer.writeheader()
        for t in txns:
            writer.writerow({
                "date": t["date"].strftime("%Y-%m-%d"),
                "description": t["description"],
                "amount": t["amount"],
                "direction": t["direction"].value,
                "category": t["category"].value,
            })
    print(f"  CSV exported → {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GCC Synthetic Data Generator")
    parser.add_argument("--mode", choices=["db", "csv", "both"], default="both")
    parser.add_argument("--smes", type=int, default=5, help="Number of SME profiles to seed")
    parser.add_argument("--scenario", choices=[s.value for s in Scenario], default=None)
    args = parser.parse_args()

    print("\n=== GCC Underwriting Engine — Synthetic Data Generator ===\n")

    if args.mode in ("db", "both"):
        print("Seeding database...")
        seed_database(num_smes=args.smes)

    if args.mode in ("csv", "both"):
        print("Exporting CSVs...")
        scenarios = [Scenario(args.scenario)] if args.scenario else list(Scenario)
        for s in scenarios:
            export_csv(s)

    print("Done.\n")
