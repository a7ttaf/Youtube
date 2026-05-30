from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


class FinanceBase(DeclarativeBase):
    """Declarative base for finance tables sharing organization metadata."""

    metadata = OrgBase.metadata


# Shared server_default for the tenant_id column added in migration
# 20260517_0001. See backend/ums_smart_revenue/db/security_models.py for
# the rationale (single source of truth for the UMS tenant id).
_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")
_TENANT_ID_DEFAULT_VALUE = UUID(UMS_TENANT_ID)


class FinanceMonthCloseORM(FinanceBase):
    """Finance month lock state for tenant-scoped close workflows."""

    __tablename__ = "finance_month_close"

    month: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OPEN'"))
    allocation_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    allocation_rule_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    locked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlocked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "month",
            name="pk_finance_month_close",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_finance_month_close_month_format",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'LOCKED')", name="ck_finance_month_close_status"
        ),
        Index("ix_finance_month_close_tenant_id", "tenant_id"),
    )


class MonthlyChannelRevenueFactORM(FinanceBase):
    """Tenant-scoped monthly channel revenue fact from trusted sources."""

    __tablename__ = "monthly_channel_revenue_facts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gross_revenue_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    net_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    shorts_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    longform_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    subscription_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    watch_time_minutes: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("0"),
    )
    confidence_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        server_default=text("1"),
    )
    imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "youtube_channel_id"],
            ["youtube_channels.tenant_id", "youtube_channels.youtube_channel_id"],
            name="fk_monthly_channel_revenue_facts_tenant_channel",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "month",
            "youtube_channel_id",
            "source_kind",
            name="uq_monthly_channel_revenue_source",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_monthly_channel_revenue_facts_month_format",
        ),
        CheckConstraint(
            "source_kind IN ("
            "'YOUTUBE_CMS', 'YOUTUBE_ANALYTICS', 'ADSENSE', "
            "'MANUAL_UPLOAD', 'ALLOCATION'"
            ")",
            name="ck_monthly_channel_revenue_facts_source_kind",
        ),
        CheckConstraint(
            "gross_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_gross_nonnegative",
        ),
        CheckConstraint(
            "net_revenue_usd IS NULL OR net_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_net_nonnegative",
        ),
        CheckConstraint(
            "shorts_revenue_usd IS NULL OR shorts_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_shorts_nonnegative",
        ),
        CheckConstraint(
            "longform_revenue_usd IS NULL OR longform_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_longform_nonnegative",
        ),
        CheckConstraint(
            "subscription_revenue_usd IS NULL OR subscription_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_subscription_nonnegative",
        ),
        CheckConstraint(
            "COALESCE(shorts_revenue_usd, 0) + "
            "COALESCE(longform_revenue_usd, 0) + "
            "COALESCE(subscription_revenue_usd, 0) <= gross_revenue_usd",
            name="ck_monthly_channel_revenue_facts_format_total",
        ),
        CheckConstraint("views >= 0", name="ck_monthly_channel_revenue_facts_views_nonnegative"),
        CheckConstraint(
            "watch_time_minutes >= 0",
            name="ck_monthly_channel_revenue_facts_watch_time_nonnegative",
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_monthly_channel_revenue_facts_confidence_range",
        ),
        Index("ix_monthly_channel_revenue_facts_month", "month"),
        Index(
            "ix_monthly_channel_revenue_facts_channel_month",
            "youtube_channel_id",
            "month",
        ),
        Index("ix_monthly_channel_revenue_facts_tenant_id", "tenant_id"),
    )


class RevenueManualOverrideORM(FinanceBase):
    """Manual revenue adjustment request with approval-state constraints."""

    __tablename__ = "revenue_manual_overrides"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    adjustment_revenue_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "youtube_channel_id"],
            ["youtube_channels.tenant_id", "youtube_channels.youtube_channel_id"],
            name="fk_revenue_manual_overrides_tenant_channel",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 1) BETWEEN '0' AND '1' "
            "AND substr(month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_revenue_manual_overrides_month_format",
        ),
        CheckConstraint(
            "adjustment_revenue_usd <> 0",
            name="ck_revenue_manual_overrides_adjustment_nonzero",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_revenue_manual_overrides_status",
        ),
        CheckConstraint(
            "(status = 'APPROVED' AND approved_by IS NOT NULL "
            "AND approved_at IS NOT NULL "
            "AND approval_reason IS NOT NULL) "
            "OR (status <> 'APPROVED' AND approved_by IS NULL AND approved_at IS NULL "
            "AND approval_reason IS NULL)",
            name="ck_revenue_manual_overrides_approval_fields",
        ),
        Index("ix_revenue_manual_overrides_month_status", "month", "status"),
        Index(
            "ix_revenue_manual_overrides_channel_month", "youtube_channel_id", "month"
        ),
        Index("ix_revenue_manual_overrides_tenant_id", "tenant_id"),
    )


class BankReconciliationEntryORM(FinanceBase):
    """Bank receipt evidence used for payment reconciliation."""

    __tablename__ = "bank_reconciliation_entries"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    bank_reference: Mapped[str] = mapped_column(Text, nullable=False)
    bank_received_date: Mapped[date] = mapped_column(Date, nullable=False)
    bank_received_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
    )
    bank_received_currency: Mapped[str] = mapped_column(Text, nullable=False)
    bank_received_amount_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
    )
    transfer_fee_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
        server_default=text("0"),
    )
    fx_difference_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
        server_default=text("0"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "month",
            "bank_reference",
            name="uq_bank_reconciliation_month_reference",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_bank_reconciliation_month_format",
        ),
        CheckConstraint(
            "bank_received_amount >= 0",
            name="ck_bank_reconciliation_received_nonnegative",
        ),
        CheckConstraint(
            "bank_received_amount_usd >= 0",
            name="ck_bank_reconciliation_received_usd_nonnegative",
        ),
        CheckConstraint(
            "transfer_fee_usd >= 0",
            name="ck_bank_reconciliation_transfer_fee_nonnegative",
        ),
        CheckConstraint(
            "length(bank_received_currency) = 3 "
            "AND bank_received_currency = upper(bank_received_currency) "
            "AND substr(bank_received_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(bank_received_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(bank_received_currency, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_bank_reconciliation_currency_code",
        ),
        Index("ix_bank_reconciliation_entries_month", "month"),
        Index("ix_bank_reconciliation_entries_source_report", "source_report_id"),
        Index("ix_bank_reconciliation_entries_tenant_id", "tenant_id"),
    )


class AdSensePaymentORM(FinanceBase):
    """Account-scoped AdSense payment source-of-truth row."""

    __tablename__ = "adsense_payments"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    payment_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    payment_currency: Mapped[str] = mapped_column(Text, nullable=False)
    payment_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PAID",
        server_default=text("'PAID'"),
    )
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_account_id", "month", "payment_name",
            name="uq_adsense_payments_account_month_name",
        ),
        CheckConstraint(
            "length(source_account_id) >= 1",
            name="ck_adsense_payments_source_account_id_nonempty",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_adsense_payments_month_format",
        ),
        CheckConstraint(
            "payment_amount >= 0",
            name="ck_adsense_payments_amount_nonnegative",
        ),
        CheckConstraint(
            "length(payment_currency) = 3 "
            "AND payment_currency = upper(payment_currency) "
            "AND substr(payment_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(payment_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(payment_currency, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_adsense_payments_currency_code",
        ),
        CheckConstraint(
            "payment_status IN ('PAID', 'PENDING', 'UNPAID', 'CANCELLED')",
            name="ck_adsense_payments_payment_status",
        ),
        Index(
            "ix_adsense_payments_tenant_month_payment_date",
            "tenant_id",
            "month",
            "payment_date",
        ),
        Index("ix_adsense_payments_month_date", "month", "payment_date"),
        Index("ix_adsense_payments_source_report", "source_report_id"),
        Index("ix_adsense_payments_tenant_id", "tenant_id"),
    )


class CurrencyExchangeRateORM(FinanceBase):
    """Imported currency rate used as external FX evidence."""

    __tablename__ = "currency_exchange_rates"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)
    quote_currency: Mapped[str] = mapped_column(Text, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    provider_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=dict,
    )
    imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "rate_date",
            "base_currency",
            "quote_currency",
            "provider_key",
            name="uq_currency_exchange_rate_provider_date_pair",
        ),
        CheckConstraint(
            "length(base_currency) = 3 "
            "AND base_currency = upper(base_currency) "
            "AND substr(base_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(base_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(base_currency, 3, 1) BETWEEN 'A' AND 'Z' "
            "AND length(quote_currency) = 3 "
            "AND quote_currency = upper(quote_currency) "
            "AND substr(quote_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(quote_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(quote_currency, 3, 1) BETWEEN 'A' AND 'Z' "
            "AND base_currency <> quote_currency",
            name="ck_currency_exchange_rates_currency_pair",
        ),
        CheckConstraint("rate > 0", name="ck_currency_exchange_rates_rate_positive"),
        Index(
            "ix_currency_exchange_rates_pair_date",
            "base_currency",
            "quote_currency",
            "rate_date",
        ),
        Index("ix_currency_exchange_rates_provider", "provider_key"),
        Index("ix_currency_exchange_rates_source_report", "source_report_id"),
    )


class DeductionComponentORM(FinanceBase):
    """Source-reported, strictly-labeled deduction-evidence component."""

    # ========================================================================
    # Purpose: One typed, source-labeled deduction-evidence row (TAX, DEDUCTION,
    #   TRANSFER_FEE, signed FX_VARIANCE, or UNRESOLVED_PAYMENT_GAP) at CHANNEL,
    #   ACCOUNT, or PAYMENT scope. Substrate only — no allocation, no net math.
    # Database/ORM: deduction_components (FinanceBase). Tenant-scoped; idempotent
    #   on (tenant_id, component_key).
    # Standards: signed finite amounts (Postgres NaN-guarded via ddl_if);
    #   object-only NOT NULL raw_payload; USD-only amounts (non-USD skipped at
    #   ingestion, never converted).
    # Blast Radius: Finance source-of-truth (new table; additive). No auth/Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> writer.
    #   - File: Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md
    # ========================================================================
    __tablename__ = "deduction_components"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    component_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    amount_native: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    component_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], [TenantORM.id],
            name="fk_deduction_components_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "component_key", name="uq_deduction_components_key"
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_deduction_components_month_format",
        ),
        CheckConstraint(
            "component_kind IN ('TAX', 'DEDUCTION', 'TRANSFER_FEE', "
            "'FX_VARIANCE', 'UNRESOLVED_PAYMENT_GAP')",
            name="ck_deduction_components_kind",
        ),
        CheckConstraint(
            "scope_kind IN ('CHANNEL', 'ACCOUNT', 'PAYMENT')",
            name="ck_deduction_components_scope_kind",
        ),
        CheckConstraint(
            "currency_code = 'USD'",
            name="ck_deduction_components_currency_code",
        ),
        CheckConstraint(
            "length(scope_id) >= 1", name="ck_deduction_components_scope_id_nonempty"
        ),
        CheckConstraint(
            "length(component_key) >= 1",
            name="ck_deduction_components_component_key_nonempty",
        ),
        # Postgres NUMERIC can store NaN (and `>= 0` would admit it); these
        # finite bounds reject NaN + ±Infinity for the signed amounts. Postgres-
        # only: the 'Infinity'::numeric cast is invalid SQLite (create_all path).
        CheckConstraint(
            "amount_usd > '-Infinity'::numeric AND amount_usd < 'Infinity'::numeric",
            name="ck_deduction_components_amount_usd_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "amount_native IS NULL OR (amount_native > '-Infinity'::numeric "
            "AND amount_native < 'Infinity'::numeric)",
            name="ck_deduction_components_amount_native_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object'",
            name="ck_deduction_components_raw_payload_object",
        ).ddl_if(dialect="postgresql"),
        Index("ix_deduction_components_tenant_month", "tenant_id", "month"),
        Index(
            "ix_deduction_components_tenant_scope",
            "tenant_id", "scope_kind", "scope_id",
        ),
        Index(
            "ix_deduction_components_tenant_month_kind",
            "tenant_id", "month", "component_kind",
        ),
    )
