"""Revenue fact repository and API serialization helpers."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class RevenueFactSourceKind(StrEnum):
    """Enumeration of possible sources for revenue facts."""

    YOUTUBE_CMS = "YOUTUBE_CMS"
    YOUTUBE_ANALYTICS = "YOUTUBE_ANALYTICS"
    ADSENSE = "ADSENSE"
    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    ALLOCATION = "ALLOCATION"


@dataclass(frozen=True)
class RevenueFactEntry:
    """Revenue fact entry with identifiers, metrics, and metadata."""

    id: str
    month: str
    youtube_channel_id: str
    source_kind: str
    source_report_id: str | None
    gross_revenue_usd: Decimal
    net_revenue_usd: Decimal | None
    views: int
    watch_time_minutes: Decimal
    confidence_score: Decimal
    imported_by: str | None
    shorts_revenue_usd: Decimal | None = None
    longform_revenue_usd: Decimal | None = None
    subscription_revenue_usd: Decimal | None = None

    @property
    def audit_entity_id(self) -> str:
        return f"{self.youtube_channel_id}:{self.month}:{self.source_kind}"

    def to_api(self) -> dict[str, object]:
        """Convert this revenue fact instance into a dictionary suitable for API
        responses.
        """
        return {
            "id": self.id,
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "source_kind": self.source_kind,
            "source_report_id": self.source_report_id,
            "gross_revenue_usd": _decimal_to_api(self.gross_revenue_usd),
            "net_revenue_usd": _decimal_to_api(self.net_revenue_usd),
            "shorts_revenue_usd": _decimal_to_api(self.shorts_revenue_usd),
            "longform_revenue_usd": _decimal_to_api(self.longform_revenue_usd),
            "subscription_revenue_usd": _decimal_to_api(self.subscription_revenue_usd),
            "views": self.views,
            "watch_time_minutes": _decimal_to_api(self.watch_time_minutes),
            "confidence_score": _decimal_to_api(self.confidence_score),
            "imported_by": self.imported_by,
        }


class RevenueFactError(ValueError):
    """Base exception for errors related to revenue fact processing."""


class RevenueFactLockedMonthError(RevenueFactError):
    """Exception raised when attempting to modify revenue facts for a locked month."""


class RevenueFactValidationError(RevenueFactError):
    """Exception raised for validation failures of revenue fact data."""


class RevenueFactNotFoundError(RevenueFactError):
    """Exception raised when a requested revenue fact is not found."""


class SqlAlchemyRevenueFactRepository:
    """SQL-backed revenue fact repository scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Initialize the repository with a database session and tenant ID."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def record_fact(
        self,
        *,
        month: str,
        youtube_channel_id: str,
        source_kind: str,
        source_report_id: str | None,
        gross_revenue_usd: Decimal,
        net_revenue_usd: Decimal | None,
        shorts_revenue_usd: Decimal | None = None,
        longform_revenue_usd: Decimal | None = None,
        subscription_revenue_usd: Decimal | None = None,
        views: int,
        watch_time_minutes: Decimal,
        confidence_score: Decimal,
        actor_user_id: str,
    ) -> RevenueFactEntry:
        """Record a revenue fact for a specified month and YouTube channel.

        Validates the month, revenue amounts, and metrics before persisting the record
        in the database and returns the resulting RevenueFactEntry.
        """
        _validate_month(month)
        _validate_revenue_amounts(
            gross_revenue_usd=gross_revenue_usd,
            net_revenue_usd=net_revenue_usd,
            shorts_revenue_usd=shorts_revenue_usd,
            longform_revenue_usd=longform_revenue_usd,
            subscription_revenue_usd=subscription_revenue_usd,
        )
        _validate_metrics(
            views=views,
            watch_time_minutes=watch_time_minutes,
            confidence_score=confidence_score,
        )
        normalized_source_kind = _normalize_source_kind(source_kind)
        actor_uuid = _actor_identity_uuid(actor_user_id)
        self._require_active_channel_for_import(youtube_channel_id)
        self._require_month_open(month)

        row = self._session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.month == month,
                MonthlyChannelRevenueFactORM.youtube_channel_id == youtube_channel_id,
                MonthlyChannelRevenueFactORM.source_kind == normalized_source_kind,
            )
        ).one_or_none()
        if row is None:
            row = MonthlyChannelRevenueFactORM(
                id=uuid4(),
                tenant_id=self._tenant_id,
                month=month,
                youtube_channel_id=youtube_channel_id,
                source_kind=normalized_source_kind,
                imported_by=actor_uuid,
            )
            self._session.add(row)

        row.source_report_id = source_report_id
        row.gross_revenue_usd = gross_revenue_usd
        row.net_revenue_usd = net_revenue_usd
        row.shorts_revenue_usd = shorts_revenue_usd
        row.longform_revenue_usd = longform_revenue_usd
        row.subscription_revenue_usd = subscription_revenue_usd
        row.views = views
        row.watch_time_minutes = watch_time_minutes
        row.confidence_score = confidence_score
        row.imported_by = actor_uuid
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_entry(row)

    def list_channel_month_facts(
        self, *, month: str, youtube_channel_id: str
    ) -> list[RevenueFactEntry]:
        """Return a list of RevenueFactEntry for a specific month and YouTube channel."""
        _validate_month(month)
        self._require_active_channel_for_read(youtube_channel_id)
        rows = self._session.scalars(
            select(MonthlyChannelRevenueFactORM)
            .where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.month == month,
                MonthlyChannelRevenueFactORM.youtube_channel_id == youtube_channel_id,
            )
            .order_by(MonthlyChannelRevenueFactORM.source_kind)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_month_facts(
        self,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[RevenueFactEntry]:
        """Return revenue facts for a month across channels."""
        _validate_month(month)
        if youtube_channel_ids == set():
            return []

        statement = (
            select(MonthlyChannelRevenueFactORM)
            .join(
                YouTubeChannelORM,
                (MonthlyChannelRevenueFactORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (
                    MonthlyChannelRevenueFactORM.youtube_channel_id
                    == YouTubeChannelORM.youtube_channel_id
                )
                & (YouTubeChannelORM.tenant_id == self._tenant_id),
            )
            .where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.month == month,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(
                MonthlyChannelRevenueFactORM.youtube_channel_id,
                MonthlyChannelRevenueFactORM.source_kind,
            )
        )
        if youtube_channel_ids is not None:
            statement = statement.where(
                MonthlyChannelRevenueFactORM.youtube_channel_id.in_(youtube_channel_ids)
            )
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)

        return [self._to_entry(row) for row in self._session.scalars(statement).all()]

    # ============================================================================
    # Purpose: Remove stale monthly revenue facts for one source kind after a
    #   reconciliation rerun proves those derived facts are no longer valid,
    #   with optional source report scoping for provenance-owned derived rows.
    # Database/ORM: MonthlyChannelRevenueFactORM (monthly_channel_revenue_facts).
    # Standards: Repository-owned SQLAlchemy DELETE; validates month/source and
    #   refuses locked months before mutating source-of-truth finance rows.
    # Blast Radius: Finance source-of-truth rows; no authorization, audit, exports,
    #   or Neo4j projection writes from this repository method.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/reconciliation_service.py -> stale
    #     OUTSIDE_CMS ALLOCATION cleanup caller.
    # ============================================================================
    def delete_month_facts(
        self,
        *,
        month: str,
        source_kind: str,
        source_report_id: str | None = None,
        youtube_channel_ids: set[str],
    ) -> int:
        """Delete selected source-kind facts in an open month.

        When provided, source_report_id scopes the delete to one provenance marker.
        """
        _validate_month(month)
        if not youtube_channel_ids:
            return 0
        normalized_source_kind = _normalize_source_kind(source_kind)
        self._require_month_open(month)
        statement = delete(MonthlyChannelRevenueFactORM).where(
            MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
            MonthlyChannelRevenueFactORM.month == month,
            MonthlyChannelRevenueFactORM.source_kind == normalized_source_kind,
            MonthlyChannelRevenueFactORM.youtube_channel_id.in_(youtube_channel_ids),
        )
        if source_report_id is not None:
            statement = statement.where(
                MonthlyChannelRevenueFactORM.source_report_id == source_report_id
            )
        result = self._session.execute(statement)
        self._session.flush()
        return int(result.rowcount or 0)

    def list_month_channel_ids(
        self,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[str]:
        """Return YouTube channel IDs that have revenue facts for a month."""
        _validate_month(month)
        if youtube_channel_ids == set():
            return []

        statement = (
            select(MonthlyChannelRevenueFactORM.youtube_channel_id)
            .join(
                YouTubeChannelORM,
                (MonthlyChannelRevenueFactORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (
                    MonthlyChannelRevenueFactORM.youtube_channel_id
                    == YouTubeChannelORM.youtube_channel_id
                )
                & (YouTubeChannelORM.tenant_id == self._tenant_id),
            )
            .where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.month == month,
                YouTubeChannelORM.active.is_(True),
            )
            .distinct()
            .order_by(MonthlyChannelRevenueFactORM.youtube_channel_id)
        )
        if youtube_channel_ids is not None:
            statement = statement.where(
                MonthlyChannelRevenueFactORM.youtube_channel_id.in_(youtube_channel_ids)
            )
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)

        return list(self._session.scalars(statement).all())

    def _require_month_open(self, month: str) -> None:
        """Ensure the given month is open for revenue fact imports, raising an
        error if the month is locked.
        """
        close = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close.status == "LOCKED":
            raise RevenueFactLockedMonthError("Finance month is locked for revenue fact imports")

    def _require_active_channel_for_import(self, youtube_channel_id: str) -> None:
        """Ensure a YouTube channel is active before import."""
        if not self._active_channel_exists(youtube_channel_id):
            raise RevenueFactValidationError("youtube_channel_id must reference an active channel")

    def _require_active_channel_for_read(self, youtube_channel_id: str) -> None:
        """
        Ensure a YouTube channel is active before reading revenue facts,
        raising not found error if not present.
        """
        if not self._active_channel_exists(youtube_channel_id):
            raise RevenueFactNotFoundError("Channel not found")

    def _active_channel_exists(self, youtube_channel_id: str) -> bool:
        """Check if a YouTube channel exists and is active for the current tenant."""
        row = self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
                YouTubeChannelORM.active.is_(True),
            )
        ).one_or_none()
        return row is not None

    @staticmethod
    def _to_entry(row: MonthlyChannelRevenueFactORM) -> RevenueFactEntry:
        """Convert a MonthlyChannelRevenueFactORM row into a RevenueFactEntry dataclass."""
        return RevenueFactEntry(
            id=str(row.id),
            month=row.month,
            youtube_channel_id=row.youtube_channel_id,
            source_kind=row.source_kind,
            source_report_id=row.source_report_id,
            gross_revenue_usd=row.gross_revenue_usd,
            net_revenue_usd=row.net_revenue_usd,
            shorts_revenue_usd=row.shorts_revenue_usd,
            longform_revenue_usd=row.longform_revenue_usd,
            subscription_revenue_usd=row.subscription_revenue_usd,
            views=row.views,
            watch_time_minutes=row.watch_time_minutes,
            confidence_score=row.confidence_score,
            imported_by=str(row.imported_by) if row.imported_by else None,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve the tenant ID to a UUID, falling back to current or default tenant."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Parse a tenant_id value into a UUID, validating format or raising a validation error."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise RevenueFactValidationError("tenant_id must be a valid UUID") from exc


def _validate_month(month: str) -> None:
    """Validate that the month string is in YYYY-MM format with a valid month component."""
    if not MONTH_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_source_kind(source_kind: str) -> str:
    """
    Normalize the source_kind string to a valid RevenueFactSourceKind
    value or raise validation error.
    """
    try:
        return RevenueFactSourceKind(source_kind).value
    except ValueError as exc:
        raise RevenueFactValidationError(
            f"Unknown revenue fact source_kind: {source_kind}"
        ) from exc


def _actor_identity_uuid(value: str) -> UUID:
    """Convert an actor identity string to a UUID, handling literal and gateway identities."""
    # Accept either a UUID literal or a trusted-gateway subject; the shared
    # helper derives a deterministic UUID5 for the latter so header-auth
    # deployments with non-UUID x-user-id values can still write revenue
    # facts. Blank values still raise (translated to the module's error).
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise RevenueFactValidationError(str(exc)) from exc


def _validate_metrics(
    *, views: int, watch_time_minutes: Decimal, confidence_score: Decimal
) -> None:
    """Validate views, watch_time_minutes, and confidence_score metrics
    for correct ranges and finiteness.
    """
    if not watch_time_minutes.is_finite():
        raise RevenueFactValidationError("watch_time_minutes must be a finite decimal")
    if not confidence_score.is_finite():
        raise RevenueFactValidationError("confidence_score must be a finite decimal")
    if views < 0:
        raise RevenueFactValidationError("views must be >= 0")
    if watch_time_minutes < 0:
        raise RevenueFactValidationError("watch_time_minutes must be >= 0")
    if confidence_score < 0 or confidence_score > 1:
        raise RevenueFactValidationError("confidence_score must be between 0 and 1")


def _validate_revenue_amounts(
    *,
    gross_revenue_usd: Decimal,
    net_revenue_usd: Decimal | None,
    shorts_revenue_usd: Decimal | None,
    longform_revenue_usd: Decimal | None,
    subscription_revenue_usd: Decimal | None,
) -> None:
    """Validate revenue amounts for finiteness, non-negativity,
    and consistency of breakdown totals.
    """
    if not gross_revenue_usd.is_finite() or gross_revenue_usd < 0:
        raise RevenueFactValidationError("gross_revenue_usd must be a finite decimal >= 0")
    if net_revenue_usd is not None and (not net_revenue_usd.is_finite() or net_revenue_usd < 0):
        raise RevenueFactValidationError("net_revenue_usd must be a finite decimal >= 0")
    format_values = (
        shorts_revenue_usd,
        longform_revenue_usd,
        subscription_revenue_usd,
    )
    if any(value is not None and (not value.is_finite() or value < 0) for value in format_values):
        raise RevenueFactValidationError(
            "revenue format breakdown values must be finite decimals >= 0"
        )
    format_total = sum((value or Decimal("0")) for value in format_values)
    if format_total > gross_revenue_usd:
        raise RevenueFactValidationError(
            "revenue format breakdown total must be <= gross_revenue_usd"
        )
