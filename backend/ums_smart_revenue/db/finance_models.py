from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, JSON, Numeric, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class FinanceBase(DeclarativeBase):
    pass


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
    youtube_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
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
