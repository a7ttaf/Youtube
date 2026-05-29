from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_FINANCE_MONTH_LOCK_KEY_PREFIX = "finance-month-close:"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


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
            "allocation_rule_payload": dict(self.allocation_rule_payload),
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
    """Persist and mutate finance month close control rows with month locks."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def get(self, month: str) -> FinanceMonthCloseEntry | None:
        """Return the close row for month, or None when no row exists."""
        row = self._session.get(
            FinanceMonthCloseORM,
            _month_close_key(month, tenant_id=self._tenant_id),
        )
        return self._to_entry(row) if row is not None else None

    def get_or_create(self, month: str) -> FinanceMonthCloseEntry:
        """Return an existing close row or create an OPEN row for month."""
        return self._to_entry(self._get_or_create_row(month))

    def lock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        """Lock month after a guarded, current readiness recheck passes."""
        from ums_smart_revenue.finance.month_close_readiness import (
            SqlAlchemyFinanceCloseReadinessService,
        )

        row = self._get_or_create_row(month, for_update=True)
        if row.status == "LOCKED":
            raise ValueError(f"Finance month is already locked: {month}")
        readiness = SqlAlchemyFinanceCloseReadinessService(
            self._session, tenant_id=self._tenant_id
        ).check_month(month, for_update=True)
        if not readiness.ready:
            raise FinanceMonthCloseReadinessError(readiness)
        row.status = "LOCKED"
        row.locked_by = _parse_uuid(actor_user_id)
        row.locked_at = datetime.now(UTC)
        row.updated_at = row.locked_at
        self._session.flush()
        return self._to_entry(row)

    def unlock_month(self, *, month: str, actor_user_id: str) -> FinanceMonthCloseEntry:
        """Reopen a locked month while holding the month close guard."""
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
        """Return the ORM row, optionally acquiring the month close guard."""
        return get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=for_update,
        )

    @staticmethod
    def _to_entry(row: FinanceMonthCloseORM) -> FinanceMonthCloseEntry:
        """Map a finance month close ORM row to its immutable entry."""
        return FinanceMonthCloseEntry(
            month=row.month,
            status=row.status,
            allocation_method=row.allocation_method,
            allocation_rule_payload=dict(row.allocation_rule_payload or {}),
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
    tenant_id: UUID | str | None = None,
    for_update: bool = False,
) -> FinanceMonthCloseORM:
    """Return or create the close row, guarding month writers when requested."""
    resolved_tenant_id = _resolve_tenant_id(tenant_id)
    if for_update:
        acquire_finance_month_advisory_lock(
            session, month, tenant_id=resolved_tenant_id
        )
    statement = select(FinanceMonthCloseORM).where(
        FinanceMonthCloseORM.tenant_id == resolved_tenant_id,
        FinanceMonthCloseORM.month == month,
    )
    if for_update:
        statement = statement.with_for_update()
    row = session.scalars(statement).one_or_none()
    if row is None:
        try:
            with session.begin_nested():
                row = FinanceMonthCloseORM(
                    tenant_id=resolved_tenant_id,
                    month=month,
                    status="OPEN",
                    allocation_rule_payload={},
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.scalars(statement).one()
    return row


# ============================================================================
# Purpose: Read-only finance month-close status lookup for the AdSense live
#   payment prefilter. Returns the close status or None for an absent row.
# Database/ORM: FinanceMonthCloseORM (SELECT only).
# Standards: Pure SELECT — no row creation, no advisory/FOR UPDATE lock, so the
#   prefilter never mutates close state or contends with month writers.
# Blast Radius: Finance month locks (read-only). The authoritative locked-month
#   write guard remains _require_month_open(..., for_update=True) in the repo.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> step-2 prefilter classifies OPEN vs LOCKED settlement months.
# ============================================================================
def get_month_close_status(
    session: Session, month: str, *, tenant_id: UUID | str | None = None
) -> str | None:
    """Return the finance close status for ``month`` (``"OPEN"``/``"LOCKED"``) or None."""
    resolved_tenant_id = _resolve_tenant_id(tenant_id)
    return session.scalar(
        select(FinanceMonthCloseORM.status).where(
            FinanceMonthCloseORM.tenant_id == resolved_tenant_id,
            FinanceMonthCloseORM.month == month,
        )
    )


def acquire_finance_month_advisory_lock(
    session: Session,
    month: str,
    *,
    tenant_id: UUID | str | None = None,
) -> None:
    """Acquire the transaction-scoped month guard used by close and writer paths."""
    if session.get_bind().dialect.name != "postgresql":
        return
    resolved_tenant_id = _resolve_tenant_id(tenant_id)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _finance_month_advisory_lock_key(month, resolved_tenant_id)},
    )


def _month_close_key(
    month: str, *, tenant_id: UUID | str | None = None
) -> tuple[UUID, str]:
    """Generate a tuple key consisting of the resolved tenant UUID and the month identifier."""
    return (_resolve_tenant_id(tenant_id), month)


def _resolve_tenant_id(
    tenant_id: UUID | str | None, *, use_context: bool = True
) -> UUID:
    """Resolve the tenant identifier from the provided value or from context, defaulting to a global tenant if none is set."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    if use_context:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Parse and validate the provided tenant_id, converting it to a UUID or raising ValueError if invalid."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("tenant_id must be a valid UUID") from exc


def _finance_month_advisory_lock_key(month: str, tenant_id: UUID) -> int:
    """Return a stable signed 64-bit advisory-lock key for a finance month."""
    digest = blake2b(
        f"{_FINANCE_MONTH_LOCK_KEY_PREFIX}{tenant_id}:{month}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)
