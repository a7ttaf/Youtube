from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM


@dataclass(frozen=True)
class FinanceMonthCloseEntry:
    month: str
    status: str
    allocation_method: str | None
    allocation_rule_payload: dict[str, object]
    locked_by: str | None
    locked_at: datetime | None
    unlocked_by: str | None
    unlocked_at: datetime | None

    def to_api(self) -> dict[str, object]:
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


class SqlAlchemyFinanceMonthCloseRepository:
    def __init__(self, session: Session):
        self._session = session

    def get_or_create(self, month: str) -> FinanceMonthCloseEntry:
        return self._to_entry(self._get_or_create_row(month))

    def lock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        row = self._get_or_create_row(month)
        if row.status == "LOCKED":
            raise ValueError(f"Finance month is already locked: {month}")
        row.status = "LOCKED"
        row.locked_by = _parse_uuid(actor_user_id)
        row.locked_at = datetime.now(UTC)
        row.updated_at = row.locked_at
        self._session.flush()
        return self._to_entry(row)

    def unlock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        row = self._get_or_create_row(month)
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
        row = self._get_or_create_row(month)
        row.allocation_method = allocation_method
        row.allocation_rule_payload = rule_payload
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_entry(row)

    def _get_or_create_row(self, month: str) -> FinanceMonthCloseORM:
        row = self._session.get(FinanceMonthCloseORM, month)
        if row is None:
            row = FinanceMonthCloseORM(month=month, status="OPEN", allocation_rule_payload={})
            self._session.add(row)
            self._session.flush()
        return row

    @staticmethod
    def _to_entry(row: FinanceMonthCloseORM) -> FinanceMonthCloseEntry:
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
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError("actor_user_id must be a valid UUID") from exc
