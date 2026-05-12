from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM


@dataclass(frozen=True)
class FinanceMonthCloseEntry:
    """Immutable API-facing state for a finance month close row."""

    month: str
    status: str
    allocation_method: str | None
    allocation_rule_payload: dict[str, object]
    locked_by: str | None
    locked_at: datetime | None
    unlocked_by: str | None
    unlocked_at: datetime | None

    def to_api(self) -> dict[str, object]:
        """Serialize the close row to the public API response shape."""
        return {
            "month": self.month,
            "status": self.status,
            "allocation_method": self.allocation_method,
            "allocation_rule_payload": self.allocation_rule_payload,
            "locked_by": self.locked_by,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
            "unlocked_by": self.unlocked_by,
            "unlocked_at": self.unlocked_at.isoformat() if self.unlocked_at else None,
        }


class FinanceMonthCloseReadinessError(ValueError):
    """Raised when a lock attempt finds unresolved close blockers."""

    def __init__(self, readiness):
        self.readiness = readiness
        super().__init__("Finance month has unresolved close blockers")


class SqlAlchemyFinanceMonthCloseRepository:
    """Persist and mutate finance month close control rows with row locks."""

    def __init__(self, session: Session):
        self._session = session

    def get(self, month: str) -> FinanceMonthCloseEntry | None:
        """Return the close row for month, or None when no row exists."""
        row = self._session.get(FinanceMonthCloseORM, month)
        return self._to_entry(row) if row is not None else None

    def get_or_create(self, month: str) -> FinanceMonthCloseEntry:
        """Return an existing close row or create an OPEN row for month."""
        return self._to_entry(self._get_or_create_row(month))

    def lock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        """Lock month after a row-locked, current readiness recheck passes."""
        from ums_smart_revenue.finance.month_close_readiness import (
            SqlAlchemyFinanceCloseReadinessService,
        )

        row = self._get_or_create_row(month, for_update=True)
        if row.status == "LOCKED":
            raise ValueError(f"Finance month is already locked: {month}")
        readiness = SqlAlchemyFinanceCloseReadinessService(self._session).check_month(
            month, for_update=True
        )
        if not readiness.ready:
            raise FinanceMonthCloseReadinessError(readiness)
        row.status = "LOCKED"
        row.locked_by = _parse_uuid(actor_user_id)
        row.locked_at = datetime.now(UTC)
        row.updated_at = row.locked_at
        self._session.flush()
        return self._to_entry(row)

    def unlock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        """Reopen a locked month while holding the close row lock."""
        row = self._get_or_create_row(month, for_update=True)
        if row.status != "LOCKED":
            raise ValueError(f"Finance month is not locked: {month}")
        row.status = "OPEN"
        row.unlocked_by = _parse_uuid(actor_user_id)
        row.unlocked_at = datetime.now(UTC)
        row.updated_at = row.unlocked_at
        self._session.flush()
        return self._to_entry(row)

    def record_allocation_rule(
        self,
        *,
        month: str,
        allocation_method: str,
        rule_payload: dict[str, object],
    ) -> FinanceMonthCloseEntry:
        """Store allocation-rule metadata when the month is still open."""
        row = self._get_or_create_row(month, for_update=True)
        if row.status == "LOCKED":
            raise ValueError(f"Finance month is locked: {month}")
        row.allocation_method = allocation_method
        row.allocation_rule_payload = rule_payload
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_entry(row)

    def _get_or_create_row(
        self, month: str, *, for_update: bool = False
    ) -> FinanceMonthCloseORM:
        """Return the ORM row, optionally acquiring a database row lock."""
        return get_or_create_month_close_row(
            self._session, month, for_update=for_update
        )

    @staticmethod
    def _to_entry(row: FinanceMonthCloseORM) -> FinanceMonthCloseEntry:
        """Map a finance month close ORM row to its immutable entry."""
        return FinanceMonthCloseEntry(
            month=row.month,
            status=row.status,
            allocation_method=row.allocation_method,
            allocation_rule_payload=row.allocation_rule_payload or {},
            locked_by=str(row.locked_by) if row.locked_by else None,
            locked_at=row.locked_at,
            unlocked_by=str(row.unlocked_by) if row.unlocked_by else None,
            unlocked_at=row.unlocked_at,
        )


def _parse_uuid(value: str) -> UUID:
    """Parse an actor UUID string for close-row audit fields."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError("actor_user_id must be a valid UUID") from exc


def get_or_create_month_close_row(
    session: Session,
    month: str,
    *,
    for_update: bool = False,
) -> FinanceMonthCloseORM:
    """Return or create the close row, using a savepoint for insert races."""
    statement = select(FinanceMonthCloseORM).where(FinanceMonthCloseORM.month == month)
    if for_update:
        statement = statement.with_for_update()
    row = session.scalars(statement).one_or_none()
    if row is None:
        try:
            with session.begin_nested():
                row = FinanceMonthCloseORM(
                    month=month, status="OPEN", allocation_rule_payload={}
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.scalars(statement).one()
    return row
