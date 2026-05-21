from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Numeric, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


class ExplanationBase(DeclarativeBase):
    pass


# Shared server_default and Python-side default for the tenant_id column added
# in migration 20260517_0001. Both are required so new ORM instances carry the
# UUID immediately, before flush.
_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")
_TENANT_ID_DEFAULT_VALUE = UUID(UMS_TENANT_ID)


class NumberExplanationORM(ExplanationBase):
    __tablename__ = "number_explanations"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'USD'")
    )
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    components: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    warnings: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
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
        UniqueConstraint(
            "tenant_id",
            "month",
            "entity_type",
            "entity_id",
            "metric",
            name="uq_number_explanations_entity_metric_month",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_number_explanations_month_format",
        ),
        CheckConstraint(
            "entity_type IN ('channel', 'company', 'sector', 'holding')",
            name="ck_number_explanations_entity_type",
        ),
        CheckConstraint("currency = 'USD'", name="ck_number_explanations_currency_usd"),
        Index("ix_number_explanations_entity", "entity_type", "entity_id", "month"),
        Index("ix_number_explanations_tenant_id", "tenant_id"),
    )
