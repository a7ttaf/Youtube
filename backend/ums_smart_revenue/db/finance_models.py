from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.db.org_models import OrgBase


class FinanceBase(DeclarativeBase):
    metadata = OrgBase.metadata


class FinanceMonthCloseORM(FinanceBase):
    __tablename__ = "finance_month_close"

    month: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OPEN'"))
    allocation_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    allocation_rule_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    locked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlocked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_finance_month_close_month_format",
        ),
        CheckConstraint("status IN ('OPEN', 'LOCKED')", name="ck_finance_month_close_status"),
    )


class MonthlyChannelRevenueFactORM(FinanceBase):
    __tablename__ = "monthly_channel_revenue_facts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    month: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("youtube_channels.youtube_channel_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gross_revenue_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    net_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    watch_time_minutes: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default=text("0"))
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("1"))
    imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
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
            "source_kind IN ('YOUTUBE_CMS', 'YOUTUBE_ANALYTICS', 'ADSENSE', 'MANUAL_UPLOAD', 'ALLOCATION')",
            name="ck_monthly_channel_revenue_facts_source_kind",
        ),
        CheckConstraint("gross_revenue_usd >= 0", name="ck_monthly_channel_revenue_facts_gross_nonnegative"),
        CheckConstraint(
            "net_revenue_usd IS NULL OR net_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_net_nonnegative",
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
        Index("ix_monthly_channel_revenue_facts_channel_month", "youtube_channel_id", "month"),
    )


class RevenueManualOverrideORM(FinanceBase):
    __tablename__ = "revenue_manual_overrides"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    month: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("youtube_channels.youtube_channel_id", ondelete="RESTRICT"),
        nullable=False,
    )
    adjustment_revenue_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING", server_default=text("'PENDING'"))
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
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
        CheckConstraint("adjustment_revenue_usd <> 0", name="ck_revenue_manual_overrides_adjustment_nonzero"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_revenue_manual_overrides_status"),
        CheckConstraint(
            "(status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL "
            "AND approval_reason IS NOT NULL) "
            "OR (status <> 'APPROVED' AND approved_by IS NULL AND approved_at IS NULL "
            "AND approval_reason IS NULL)",
            name="ck_revenue_manual_overrides_approval_fields",
        ),
        Index("ix_revenue_manual_overrides_month_status", "month", "status"),
        Index("ix_revenue_manual_overrides_channel_month", "youtube_channel_id", "month"),
    )


class AdSensePaymentORM(FinanceBase):
    __tablename__ = "adsense_payments"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    payment_name: Mapped[str] = mapped_column(Text, nullable=False)
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
        JSON,
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

    __table_args__ = (
        UniqueConstraint(
            "month",
            "payment_name",
            name="uq_adsense_payments_month_name",
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
            "AND payment_currency = upper(payment_currency)",
            name="ck_adsense_payments_currency_code",
        ),
        CheckConstraint(
            "payment_status IN ('PAID', 'PENDING', 'UNPAID', 'CANCELLED')",
            name="ck_adsense_payments_payment_status",
        ),
        Index("ix_adsense_payments_month_date", "month", "payment_date"),
        Index("ix_adsense_payments_source_report", "source_report_id"),
    )
