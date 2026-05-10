from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import re
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM, RevenueManualOverrideORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM


MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class RevenueManualOverrideEntry:
    id: str
    month: str
    youtube_channel_id: str
    adjustment_revenue_usd: Decimal
    reason: str
    status: str
    created_by: str
    approved_by: str | None
    approval_reason: str | None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "adjustment_revenue_usd": _decimal_to_api(self.adjustment_revenue_usd),
            "reason": self.reason,
            "status": self.status,
            "created_by": self.created_by,
            "approved_by": self.approved_by,
            "approval_reason": self.approval_reason,
        }


class ManualOverrideError(ValueError):
    pass


class ManualOverrideConflictError(ManualOverrideError):
    pass


class ManualOverrideLockedMonthError(ManualOverrideError):
    pass


class ManualOverrideNotFoundError(ManualOverrideError):
    pass


class ManualOverrideValidationError(ManualOverrideError):
    pass


class SqlAlchemyManualOverrideRepository:
    def __init__(self, session: Session):
        self._session = session

    def create_override(
        self,
        *,
        month: str,
        youtube_channel_id: str,
        adjustment_revenue_usd: Decimal,
        reason: str,
        actor_user_id: str,
    ) -> RevenueManualOverrideEntry:
        _validate_month(month)
        _validate_nonzero_adjustment(adjustment_revenue_usd)
        normalized_reason = _normalize_reason(reason)
        actor_uuid = _parse_uuid(actor_user_id)
        self._require_active_channel(youtube_channel_id)
        self._require_month_open(month)

        row = RevenueManualOverrideORM(
            id=uuid4(),
            month=month,
            youtube_channel_id=youtube_channel_id,
            adjustment_revenue_usd=adjustment_revenue_usd,
            reason=normalized_reason,
            status="PENDING",
            created_by=actor_uuid,
            updated_at=datetime.now(UTC),
        )
        self._session.add(row)
        self._session.flush()
        return self._to_entry(row)

    def get_override(self, override_id: str) -> RevenueManualOverrideEntry:
        row = self._get_row(override_id)
        return self._to_entry(row)

    def list_channel_month_overrides(self, *, month: str, youtube_channel_id: str) -> list[RevenueManualOverrideEntry]:
        _validate_month(month)
        rows = self._session.scalars(
            select(RevenueManualOverrideORM)
            .where(
                RevenueManualOverrideORM.month == month,
                RevenueManualOverrideORM.youtube_channel_id == youtube_channel_id,
            )
            .order_by(RevenueManualOverrideORM.created_at, RevenueManualOverrideORM.id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def approve_override(
        self,
        *,
        override_id: str,
        actor_user_id: str,
        reason: str,
    ) -> RevenueManualOverrideEntry:
        actor_uuid = _parse_uuid(actor_user_id)
        normalized_reason = _normalize_reason(reason)
        row = self._get_row(override_id)
        self._require_month_open(row.month)
        if row.status != "PENDING":
            raise ManualOverrideConflictError("Manual override cannot be approved from its current state")
        if row.created_by == actor_uuid:
            raise ManualOverrideValidationError("Manual override creator cannot approve their own override")

        row.status = "APPROVED"
        row.approved_by = actor_uuid
        row.approved_at = datetime.now(UTC)
        row.approval_reason = normalized_reason
        row.updated_at = row.approved_at
        self._session.flush()
        return self._to_entry(row)

    def _get_row(self, override_id: str) -> RevenueManualOverrideORM:
        override_uuid = _parse_uuid(override_id, field_name="manual_override_id")
        row = self._session.get(RevenueManualOverrideORM, override_uuid)
        if row is None:
            raise ManualOverrideNotFoundError("Manual override not found")
        return row

    def _require_active_channel(self, youtube_channel_id: str) -> None:
        row = self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
                YouTubeChannelORM.active.is_(True),
            )
        ).one_or_none()
        if row is None:
            raise ManualOverrideValidationError("youtube_channel_id must reference an active channel")

    def _require_month_open(self, month: str) -> None:
        close = self._session.get(FinanceMonthCloseORM, month)
        if close is not None and close.status == "LOCKED":
            raise ManualOverrideLockedMonthError("Finance month is locked for manual overrides")

    @staticmethod
    def _to_entry(row: RevenueManualOverrideORM) -> RevenueManualOverrideEntry:
        return RevenueManualOverrideEntry(
            id=str(row.id),
            month=row.month,
            youtube_channel_id=row.youtube_channel_id,
            adjustment_revenue_usd=row.adjustment_revenue_usd,
            reason=row.reason,
            status=row.status,
            created_by=str(row.created_by),
            approved_by=str(row.approved_by) if row.approved_by else None,
            approval_reason=row.approval_reason,
        )


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise ManualOverrideValidationError("month must use YYYY-MM with a calendar month from 01 to 12")


def _validate_nonzero_adjustment(value: Decimal) -> None:
    if value == 0:
        raise ManualOverrideValidationError("adjustment_revenue_usd must not be zero")


def _normalize_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise ManualOverrideValidationError("reason must not be blank")
    return normalized


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ManualOverrideValidationError(f"{field_name} must be a valid UUID") from exc


def _decimal_to_api(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
