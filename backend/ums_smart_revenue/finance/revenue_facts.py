import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class RevenueFactSourceKind(StrEnum):
    YOUTUBE_CMS = "YOUTUBE_CMS"
    YOUTUBE_ANALYTICS = "YOUTUBE_ANALYTICS"
    ADSENSE = "ADSENSE"
    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    ALLOCATION = "ALLOCATION"


@dataclass(frozen=True)
class RevenueFactEntry:
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

    @property
    def audit_entity_id(self) -> str:
        return f"{self.youtube_channel_id}:{self.month}:{self.source_kind}"

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "source_kind": self.source_kind,
            "source_report_id": self.source_report_id,
            "gross_revenue_usd": _decimal_to_api(self.gross_revenue_usd),
            "net_revenue_usd": _decimal_to_api(self.net_revenue_usd),
            "views": self.views,
            "watch_time_minutes": _decimal_to_api(self.watch_time_minutes),
            "confidence_score": _decimal_to_api(self.confidence_score),
            "imported_by": self.imported_by,
        }


class RevenueFactError(ValueError):
    pass


class RevenueFactLockedMonthError(RevenueFactError):
    pass


class RevenueFactValidationError(RevenueFactError):
    pass


class RevenueFactNotFoundError(RevenueFactError):
    pass


class SqlAlchemyRevenueFactRepository:
    """SQL-backed revenue fact repository scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
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
        views: int,
        watch_time_minutes: Decimal,
        confidence_score: Decimal,
        actor_user_id: str,
    ) -> RevenueFactEntry:
        _validate_month(month)
        _validate_revenue_amounts(
            gross_revenue_usd=gross_revenue_usd,
            net_revenue_usd=net_revenue_usd,
        )
        _validate_metrics(
            views=views,
            watch_time_minutes=watch_time_minutes,
            confidence_score=confidence_score,
        )
        normalized_source_kind = _normalize_source_kind(source_kind)
        actor_uuid = _parse_uuid(actor_user_id)
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

    def list_month_channel_ids(
        self,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[str]:
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
        close = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close.status == "LOCKED":
            raise RevenueFactLockedMonthError(
                "Finance month is locked for revenue fact imports"
            )

    def _require_active_channel_for_import(self, youtube_channel_id: str) -> None:
        if not self._active_channel_exists(youtube_channel_id):
            raise RevenueFactValidationError(
                "youtube_channel_id must reference an active channel"
            )

    def _require_active_channel_for_read(self, youtube_channel_id: str) -> None:
        if not self._active_channel_exists(youtube_channel_id):
            raise RevenueFactNotFoundError("Channel not found")

    def _active_channel_exists(self, youtube_channel_id: str) -> bool:
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
        return RevenueFactEntry(
            id=str(row.id),
            month=row.month,
            youtube_channel_id=row.youtube_channel_id,
            source_kind=row.source_kind,
            source_report_id=row.source_report_id,
            gross_revenue_usd=row.gross_revenue_usd,
            net_revenue_usd=row.net_revenue_usd,
            views=row.views,
            watch_time_minutes=row.watch_time_minutes,
            confidence_score=row.confidence_score,
            imported_by=str(row.imported_by) if row.imported_by else None,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    if tenant_id is not None:
        return UUID(tenant_id) if isinstance(tenant_id, str) else tenant_id
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_source_kind(source_kind: str) -> str:
    try:
        return RevenueFactSourceKind(source_kind).value
    except ValueError as exc:
        raise RevenueFactValidationError(
            f"Unknown revenue fact source_kind: {source_kind}"
        ) from exc


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise RevenueFactValidationError("actor_user_id must be a valid UUID") from exc


def _validate_metrics(
    *, views: int, watch_time_minutes: Decimal, confidence_score: Decimal
) -> None:
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
    *, gross_revenue_usd: Decimal, net_revenue_usd: Decimal | None
) -> None:
    if not gross_revenue_usd.is_finite() or gross_revenue_usd < 0:
        raise RevenueFactValidationError(
            "gross_revenue_usd must be a finite decimal >= 0"
        )
    if net_revenue_usd is not None and (
        not net_revenue_usd.is_finite() or net_revenue_usd < 0
    ):
        raise RevenueFactValidationError(
            "net_revenue_usd must be a finite decimal >= 0"
        )


def _decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
