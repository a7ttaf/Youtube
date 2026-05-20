from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_SQLITE_TENANT_CHANNEL_UNIQUE_ERROR = (
    "unique constraint failed: youtube_channels.tenant_id, "
    "youtube_channels.youtube_channel_id"
)


class SqlAlchemyChannelRegistry:
    """SQL-backed channel registry scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def list_channels(self) -> list[ChannelRegistryEntry]:
        """Return active channels in the bound tenant."""
        rows = self._session.scalars(
            select(YouTubeChannelORM)
            .where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str]
    ) -> list[ChannelRegistryEntry]:
        """Return active channels matching a set of external channel ids."""
        if not youtube_channel_ids:
            return []
        rows = self._session.scalars(
            select(YouTubeChannelORM)
            .where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.active.is_(True),
                YouTubeChannelORM.youtube_channel_id.in_(youtube_channel_ids),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        """Return a single channel entry by external id, or None."""
        row = self._get_row(youtube_channel_id)
        if row is None:
            return None
        return self._to_entry(row)

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        """Create a channel row, raising on tenant-scoped duplicate or FK violation."""
        if self._get_row(youtube_channel_id) is not None:
            raise ChannelRegistryConflictError(
                f"Channel already exists: {youtube_channel_id}"
            )

        row = YouTubeChannelORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            primary_org_unit_id=_parse_optional_uuid(
                primary_company_id, "primary_company_id"
            ),
            cms_status=cms_status,
            revenue_required=revenue_required,
            active=True,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            if _is_duplicate_channel_integrity_error(exc):
                raise ChannelRegistryConflictError(
                    f"Channel already exists: {youtube_channel_id}"
                ) from exc
            raise _channel_registry_validation_error_from_integrity_error(exc) from exc
        return self._to_entry(row)

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise KeyError(f"Channel not found: {youtube_channel_id}")

        row.primary_org_unit_id = _parse_optional_uuid(
            primary_company_id, "primary_company_id"
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise _channel_registry_validation_error_from_integrity_error(exc) from exc
        return self._to_entry(row)

    def _get_row(self, youtube_channel_id: str) -> YouTubeChannelORM | None:
        """Look up the ORM row filtered by tenant_id + external channel id."""
        return self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
            )
        ).one_or_none()

    @staticmethod
    def _to_entry(row: YouTubeChannelORM) -> ChannelRegistryEntry:
        return ChannelRegistryEntry(
            youtube_channel_id=row.youtube_channel_id,
            channel_name=row.channel_name,
            primary_company_id=str(row.primary_org_unit_id)
            if row.primary_org_unit_id is not None
            else None,
            cms_status=row.cms_status,
            revenue_required=row.revenue_required,
            active=row.active,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or default fallback."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ChannelRegistryValidationError("tenant_id must be a valid UUID") from exc


def _parse_optional_uuid(value: str | None, field_name: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise ChannelRegistryValidationError(
            f"{field_name} must be a valid UUID"
        ) from exc


def _is_duplicate_channel_integrity_error(exc: IntegrityError) -> bool:
    constraint_name = _constraint_name(exc)
    error_text = _integrity_error_text(exc)
    return (
        "youtube_channel_id" in constraint_name
        or (
            "youtube_channels" in constraint_name
            and "youtube_channel" in constraint_name
        )
        or "unique constraint failed: youtube_channels.youtube_channel_id" in error_text
        or _SQLITE_TENANT_CHANNEL_UNIQUE_ERROR in error_text
    )


def _channel_registry_validation_error_from_integrity_error(
    exc: IntegrityError,
) -> ChannelRegistryValidationError:
    constraint_name = _constraint_name(exc)
    error_text = _integrity_error_text(exc)
    if (
        "primary_org_unit_id" in constraint_name
        or "tenant_org_unit" in constraint_name
        or "foreign key constraint failed" in error_text
    ):
        return ChannelRegistryValidationError(
            "primary_company_id must reference an existing org unit"
        )
    return ChannelRegistryValidationError(
        "Channel registry values violate database constraints"
    )


def _constraint_name(exc: IntegrityError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return str(getattr(diag, "constraint_name", "") or "").lower()


def _integrity_error_text(exc: IntegrityError) -> str:
    return f"{exc.orig!s} {exc!s}".lower()
