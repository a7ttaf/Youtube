"""SQLAlchemy ORM models for source-reported revenue ingestion.

Tables defined here register on FinanceBase.metadata so they share the
Alembic target metadata that env.py already imports for finance models.
CurrencyORM is platform-wide reference data with no tenant column.
GoogleRevenueSourceRowORM is tenant-scoped per spec section 4.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ums_smart_revenue.db.finance_models import FinanceBase


# ============================================================================
# Purpose: Platform-wide ISO 4217 currency reference table.
# Database/ORM: currencies table; FinanceBase metadata.
# Standards: Format checks enforce 3-uppercase-letter codes and 3-digit
#            numeric codes. minor_unit may be NULL only for non-applicable
#            ISO entries (funds, precious metals, test codes); supported
#            rows must declare a known minor_unit. activated_at is set
#            when is_supported flips to TRUE.
# Blast Radius: Reference data only — read by validation and seeds. No
#               graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/db/iso_4217_2026_05.py -> Seed source.
# ============================================================================
class CurrencyORM(FinanceBase):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    numeric_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    minor_unit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_supported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "length(code) = 3 "
            "AND code = upper(code) "
            "AND substr(code, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(code, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(code, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_currencies_code_format",
        ),
        CheckConstraint(
            "length(numeric_code) = 3 "
            "AND substr(numeric_code, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(numeric_code, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(numeric_code, 3, 1) BETWEEN '0' AND '9'",
            name="ck_currencies_numeric_code_format",
        ),
        UniqueConstraint("numeric_code", name="uq_currencies_numeric_code"),
        CheckConstraint(
            "minor_unit IS NULL OR (minor_unit BETWEEN 0 AND 6)",
            name="ck_currencies_minor_unit_range",
        ),
        CheckConstraint(
            "is_supported = false OR minor_unit IS NOT NULL",
            name="ck_currencies_supported_minor",
        ),
        CheckConstraint(
            "is_supported = false OR activated_at IS NOT NULL",
            name="ck_currencies_supported_activated",
        ),
    )


# GoogleRevenueSourceRowORM ships in Task 2.2.
