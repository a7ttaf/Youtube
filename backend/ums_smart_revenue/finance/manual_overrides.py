import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import RevenueManualOverrideORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class RevenueManualOverrideEntry:
    """Represents a manual revenue override entry with its details and conversion to API format."""
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
        """Convert this manual override instance into a dictionary formatted for API responses.

        Returns:
            dict[str, object]: A dictionary containing the override's attributes in a serializable form.
        """
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
    """Base exception for manual override related errors."""


class ManualOverrideConflictError(ManualOverrideError):
    """Raised when a manual override conflicts with an existing override."""


class ManualOverrideLockedMonthError(ManualOverrideError):
    """Raised when attempting a manual override on a locked month."""


class ManualOverrideNotFoundError(ManualOverrideError):
    """Raised when a specified manual override entry is not found."""


class ManualOverrideValidationError(ManualOverrideError):
    """Raised when manual override parameters fail validation checks."""


class SqlAlchemyManualOverrideRepository:
    """SQL-backed manual override repository scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Initialize repository with DB session and resolve tenant identifier."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def create_override(
        self,
        *,
        month: str,
        youtube_channel_id: str,
        adjustment_revenue_usd: Decimal,
        reason: str,
        actor_user_id: str,
    ) -> RevenueManualOverrideEntry:
        """Create a manual revenue override for a specific channel and month with reason and actor."""
        _validate_month(month)
        _validate_nonzero_adjustment(adjustment_revenue_usd)
        normalized_reason = _normalize_reason(reason)
        actor_uuid = _actor_identity_uuid(actor_user_id)
        self._require_active_channel(youtube_channel_id)
        self._require_month_open(month)

        row = RevenueManualOverrideORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
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
        """Retrieve a revenue manual override entry by its ID."""
        row = self._get_row(override_id)
        return self._to_entry(row)

    def get_override_channel_id(self, override_id: str) -> str | None:
        """Retrieve the YouTube channel ID associated with a manual override."""
        override_uuid = _parse_uuid(override_id, field_name="manual_override_id")
        return self._session.scalar(
            select(RevenueManualOverrideORM.youtube_channel_id).where(
                RevenueManualOverrideORM.tenant_id == self._tenant_id,
                RevenueManualOverrideORM.id == override_uuid,
            )
        )

    def list_channel_month_overrides(
        self, *, month: str, youtube_channel_id: str
    ) -> list[RevenueManualOverrideEntry]:
        """List manual override entries for a given channel and month."""
        _validate_month(month)
        rows = self._session.scalars(
            select(RevenueManualOverrideORM)
            .where(
                RevenueManualOverrideORM.tenant_id == self._tenant_id,
                RevenueManualOverrideORM.month == month,
                RevenueManualOverrideORM.youtube_channel_id == youtube_channel_id,
            )
            .order_by(RevenueManualOverrideORM.created_at, RevenueManualOverrideORM.id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_month_overrides(
        self,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
    ) -> list[RevenueManualOverrideEntry]:
        """List manual override entries for a given month and optional set of channels."""
        _validate_month(month)
        if youtube_channel_ids == set():
            return []

        statement = (
            select(RevenueManualOverrideORM)
            .join(
                YouTubeChannelORM,
                (RevenueManualOverrideORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (
                    RevenueManualOverrideORM.youtube_channel_id
                    == YouTubeChannelORM.youtube_channel_id
                ),
            )
            .where(
                RevenueManualOverrideORM.tenant_id == self._tenant_id,
                RevenueManualOverrideORM.month == month,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(
                RevenueManualOverrideORM.youtube_channel_id,
                RevenueManualOverrideORM.created_at,
                RevenueManualOverrideORM.id,
            )
        )
        if youtube_channel_ids is not None:
            statement = statement.where(
                RevenueManualOverrideORM.youtube_channel_id.in_(youtube_channel_ids)
            )
        rows = self._session.scalars(statement).all()
        return [self._to_entry(row) for row in rows]

    def approve_override(
        self,
        *,
        override_id: str,
        actor_user_id: str,
        reason: str,
    ) -> RevenueManualOverrideEntry:
        """Approve a pending manual override, ensuring actor and month constraints."""
        actor_uuid = _actor_identity_uuid(actor_user_id)
        normalized_reason = _normalize_reason(reason)
        row = self._get_row(override_id, for_update=True)
        self._require_month_open(row.month, for_update=True)
        if row.status != "PENDING":
            raise ManualOverrideConflictError(
                "Manual override cannot be approved from its current state"
            )
        if row.created_by == actor_uuid:
            raise ManualOverrideValidationError(
                "Manual override creator cannot approve their own override"
            )

        row.status = "APPROVED"
        row.approved_by = actor_uuid
        row.approved_at = datetime.now(UTC)
        row.approval_reason = normalized_reason
        row.updated_at = row.approved_at
        self._session.flush()
        return self._to_entry(row)

    def _get_row(
        self, override_id: str, *, for_update: bool = False
    ) -> RevenueManualOverrideORM:
        """Internal helper to fetch a manual override ORM row by ID, optionally locking it."""
        override_uuid = _parse_uuid(override_id, field_name="manual_override_id")
        statement = select(RevenueManualOverrideORM).where(
            RevenueManualOverrideORM.tenant_id == self._tenant_id,
            RevenueManualOverrideORM.id == override_uuid,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalars(statement).one_or_none()
        if row is None:
            raise ManualOverrideNotFoundError("Manual override not found")
        return row

    def _require_active_channel(self, youtube_channel_id: str) -> None:
        """Internal helper to validate that a channel is active for the current tenant."""
        row = self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
                YouTubeChannelORM.active.is_(True),
            )
        ).one_or_none()
        if row is None:
            raise ManualOverrideValidationError(
                "youtube_channel_id must reference an active channel"
            )

    def _require_month_open(self, month: str, *, for_update: bool = True) -> None:
        """Internal helper to ensure the finance month is open for overrides."""
        close = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=for_update,
        )
        if close.status == "LOCKED":
            raise ManualOverrideLockedMonthError(
                "Finance month is locked for manual overrides"
            )

    @staticmethod
    def _to_entry(row: RevenueManualOverrideORM) -> RevenueManualOverrideEntry:
        """Convert a RevenueManualOverrideORM row to a RevenueManualOverrideEntry dataclass."""
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


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve or obtain the tenant UUID from input or current context."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Convert or validate a tenant UUID from a string or UUID instance."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ManualOverrideValidationError("tenant_id must be a valid UUID") from exc


def _validate_month(month: str) -> None:
    """Validate that month string matches YYYY-MM format and valid month."""
    if not MONTH_PATTERN.fullmatch(month):
        raise ManualOverrideValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _validate_nonzero_adjustment(value: Decimal) -> None:
    """Ensure the adjustment revenue decimal is finite and non-zero."""
    if not value.is_finite():
        raise ManualOverrideValidationError(
            "adjustment_revenue_usd must be a finite decimal"
        )
    if value == 0:
        raise ManualOverrideValidationError("adjustment_revenue_usd must not be zero")


def _normalize_reason(reason: str) -> str:
    """Normalize and validate the reason text, trimming whitespace."""
    normalized = reason.strip()
    if not normalized:
        raise ManualOverrideValidationError("reason must not be blank")
    return normalized


def _actor_identity_uuid(value: str) -> UUID:
    """Derive a UUID for the actor from input, accepting UUID literals or gateway IDs."""
    # Accept either a UUID literal or a trusted-gateway subject; the shared
    # helper derives a deterministic UUID5 for the latter so header-auth
    # deployments with non-UUID x-user-id values can still create or approve
    # manual overrides. Entity IDs (e.g. manual_override_id) still use the
    # strict _parse_uuid below since they must reference a real DB row.
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise ManualOverrideValidationError(str(exc)) from exc


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    """Parse and validate a UUID string for a given field name, raising on error."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise ManualOverrideValidationError(
            f"{field_name} must be a valid UUID"
        ) from exc
