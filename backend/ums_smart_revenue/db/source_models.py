"""SQLAlchemy ORM models for source-reported revenue ingestion.

Tables defined here register on FinanceBase.metadata so they share the
Alembic target metadata that env.py already imports for finance models.
CurrencyORM is platform-wide reference data with no tenant column.
GoogleRevenueSourceRowORM is tenant-scoped per spec section 4.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from ums_smart_revenue.db.finance_models import FinanceBase

# RawReportFileORM and TenantORM live on their own DeclarativeBase metadatas
# (ReportBase / TenantBase). GoogleRevenueSourceRowORM below uses direct
# Column references (TenantORM.id, RawReportFileORM.id) inside its
# ForeignKeyConstraint because SQLAlchemy 2.x cannot resolve cross-metadata
# FK targets by string name at create_all time; direct Column references
# work regardless of which MetaData hosts the referenced table.
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.db.tenant_models import TenantORM


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


# ============================================================================
# Purpose: Tenant-scoped storage for Google/YouTube/AdSense source-reported
#          monetary source rows. Idempotent on
#          (tenant_id, source_system, source_row_key); source_row_key is a
#          full 64-char SHA-256 hex digest derived from stable Google
#          identifiers + dimensions + period + report identifiers.
# Database/ORM: google_revenue_source_rows table; FinanceBase metadata.
#               tenant_id -> TenantORM.id (TenantBase metadata) and
#               raw_file_id -> RawReportFileORM.id (ReportBase metadata) use
#               direct Column references because SQLAlchemy 2.x cannot
#               resolve cross-metadata FK targets via the string form
#               ("tenants.id", "raw_report_files.id") inside create_all().
#               currency_code -> currencies.code stays as a string because
#               CurrencyORM is on FinanceBase.metadata.
# Standards: All monetary values preserved exactly as the Google source
#            reported (amount_native + currency_code). Native-precision
#            NUMERIC(20, 6) avoids float loss. raw_payload is JSONB on
#            PostgreSQL, JSON elsewhere via the with_variant pattern.
# Blast Radius: Source-of-truth table for downstream finance ingestion. No
#               graph projection impact detected.
# Connections:
#   - File: Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md -> §4 schema.
#   - File: backend/ums_smart_revenue/connectors/google_source_rows/repository.py -> Storage repository.
# ============================================================================
class GoogleRevenueSourceRowORM(FinanceBase):
    __tablename__ = "google_revenue_source_rows"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    youtube_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    report_month: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    metric_key: Mapped[str] = mapped_column(Text, nullable=False)
    value_kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_native: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    currency_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_file_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], [TenantORM.id],
            name="fk_google_revenue_source_rows_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["currency_code"], ["currencies.code"],
            name="fk_google_revenue_source_rows_currency",
        ),
        ForeignKeyConstraint(
            ["raw_file_id"], [RawReportFileORM.id],
            name="fk_google_revenue_source_rows_raw_file",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "source_system", "source_row_key",
            name="uq_google_revenue_source_rows_source_key",
        ),
        CheckConstraint(
            "amount_native >= 0",
            name="ck_google_revenue_source_rows_nonneg",
        ),
        # A NUMERIC(20,6) column already rejects ±Infinity at the type level, but
        # NaN IS storable and `>= 0` admits it (NaN sorts above every finite
        # value), so a direct-SQL / backfill / future-service writer could land a
        # NaN amount in this source-of-truth table. This finite bound rejects NaN
        # (NaN < 'Infinity' is false), mirroring the repository's is_finite()
        # guard at the schema boundary. Postgres-only via ddl_if: the literal
        # 'Infinity'::numeric is not valid SQLite, and this metadata builds the
        # SQLite test tables via create_all.
        CheckConstraint(
            "amount_native < 'Infinity'::numeric",
            name="ck_google_revenue_source_rows_amount_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "source_system IN ('youtube_reporting', 'youtube_analytics', 'adsense_management')",
            name="ck_google_revenue_source_rows_source_system",
        ),
        CheckConstraint(
            "value_kind IN ('estimated', 'settled', 'adjustment', 'tax', 'deduction')",
            name="ck_google_revenue_source_rows_value_kind",
        ),
        CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_google_revenue_source_rows_report_month_format",
        ),
        CheckConstraint(
            "period_end >= period_start",
            name="ck_google_revenue_source_rows_period_order",
        ),
        CheckConstraint(
            "length(source_row_key) = 64",
            name="ck_google_revenue_source_rows_source_row_key_length",
        ),
        # PostgreSQL (source of truth) enforces that raw_payload is a JSON
        # object, not an array/scalar/null. jsonb_typeof is PostgreSQL-only, so
        # ddl_if keeps it off the SQLite create_all() path the ORM tests use;
        # the migration applies the same CHECK guarded by dialect name.
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object'",
            name="ck_google_revenue_source_rows_raw_payload_object",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_google_revenue_source_rows_tenant_month_source",
            "tenant_id", "report_month", "source_system",
        ),
        Index(
            "ix_google_revenue_source_rows_tenant_channel_month",
            "tenant_id", "youtube_channel_id", "report_month",
            postgresql_where=text("youtube_channel_id IS NOT NULL"),
            sqlite_where=text("youtube_channel_id IS NOT NULL"),
        ),
    )
