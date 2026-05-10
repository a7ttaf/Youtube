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


class SqlAlchemyChannelRegistry:
    def __init__(self, session: Session):
        self._session = session

    def list_channels(self) -> list[ChannelRegistryEntry]:
        rows = self._session.scalars(
            select(YouTubeChannelORM)
            .where(YouTubeChannelORM.active.is_(True))
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
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
        if self._get_row(youtube_channel_id) is not None:
            raise ChannelRegistryConflictError(f"Channel already exists: {youtube_channel_id}")

        row = YouTubeChannelORM(
            id=uuid4(),
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            primary_org_unit_id=_parse_optional_uuid(primary_company_id, "primary_company_id"),
            cms_status=cms_status,
            revenue_required=revenue_required,
            active=True,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ChannelRegistryConflictError(f"Channel already exists: {youtube_channel_id}") from exc
        return self._to_entry(row)

    def update_mapping(self, *, youtube_channel_id: str, primary_company_id: str | None) -> ChannelRegistryEntry:
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise KeyError(f"Channel not found: {youtube_channel_id}")

        row.primary_org_unit_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        self._session.flush()
        return self._to_entry(row)

    def _get_row(self, youtube_channel_id: str) -> YouTubeChannelORM | None:
        return self._session.scalars(
            select(YouTubeChannelORM).where(YouTubeChannelORM.youtube_channel_id == youtube_channel_id)
        ).one_or_none()

    @staticmethod
    def _to_entry(row: YouTubeChannelORM) -> ChannelRegistryEntry:
        return ChannelRegistryEntry(
            youtube_channel_id=row.youtube_channel_id,
            channel_name=row.channel_name,
            primary_company_id=str(row.primary_org_unit_id) if row.primary_org_unit_id is not None else None,
            cms_status=row.cms_status,
            revenue_required=row.revenue_required,
            active=row.active,
        )


def _parse_optional_uuid(value: str | None, field_name: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise ChannelRegistryValidationError(f"{field_name} must be a valid UUID") from exc
