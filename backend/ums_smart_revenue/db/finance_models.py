from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, JSON, Text, Uuid, func, text
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
