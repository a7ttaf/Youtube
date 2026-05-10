from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import ChannelGroupMemberORM, ChannelGroupORM, YouTubeChannelORM
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry


class SqlAlchemyChannelGroupRegistry:
    def __init__(self, session: Session):
        self._session = session

    def list_groups(self) -> list[ChannelGroupEntry]:
        rows = self._session.scalars(
            select(ChannelGroupORM).where(ChannelGroupORM.active.is_(True)).order_by(ChannelGroupORM.name)
        ).all()
        return [self._to_entry(row) for row in rows]

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        row = self._get_group_row(group_id)
        if row is None:
            return None
        return self._to_entry(row)

    def create_group(self, *, name: str, group_type: str, channel_ids: list[str]) -> ChannelGroupEntry:
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        row = ChannelGroupORM(id=uuid4(), name=name, group_type=group_type, active=True)
        self._session.add(row)
        self._session.flush()
        self._replace_member_rows(row.id, channel_rows)
        self._session.flush()
        return self._to_entry(row)

    def update_group(self, *, group_id: str, name: str | None, active: bool | None) -> ChannelGroupEntry:
        row = self._require_group_row(group_id)
        if name is not None:
            row.name = name
        if active is not None:
            row.active = active
        self._session.flush()
        return self._to_entry(row)

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        row = self._require_group_row(group_id)
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        existing_ids = set(
            self._session.scalars(
                select(ChannelGroupMemberORM.channel_id).where(ChannelGroupMemberORM.group_id == row.id)
            ).all()
        )
        for channel in channel_rows:
            if channel.id not in existing_ids:
                self._session.add(ChannelGroupMemberORM(group_id=row.id, channel_id=channel.id))
        self._session.flush()
        return self._to_entry(row)

    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        row = self._require_group_row(group_id)
        channel = self._channel_rows_by_external_ids([channel_id])[0]
        self._session.execute(
            delete(ChannelGroupMemberORM).where(
                ChannelGroupMemberORM.group_id == row.id,
                ChannelGroupMemberORM.channel_id == channel.id,
            )
        )
        self._session.flush()
        return self._to_entry(row)

    def _replace_member_rows(self, group_id: UUID, channel_rows: list[YouTubeChannelORM]) -> None:
        self._session.execute(delete(ChannelGroupMemberORM).where(ChannelGroupMemberORM.group_id == group_id))
        for channel in channel_rows:
            self._session.add(ChannelGroupMemberORM(group_id=group_id, channel_id=channel.id))

    def _get_group_row(self, group_id: str) -> ChannelGroupORM | None:
        try:
            group_uuid = UUID(group_id)
        except (TypeError, ValueError):
            return None
        return self._session.get(ChannelGroupORM, group_uuid)

    def _require_group_row(self, group_id: str) -> ChannelGroupORM:
        row = self._get_group_row(group_id)
        if row is None:
            raise KeyError(f"Group not found: {group_id}")
        return row

    def _channel_rows_by_external_ids(self, channel_ids: list[str]) -> list[YouTubeChannelORM]:
        unique_channel_ids = list(dict.fromkeys(channel_ids))
        if not unique_channel_ids:
            return []
        rows = self._session.scalars(
            select(YouTubeChannelORM).where(YouTubeChannelORM.youtube_channel_id.in_(unique_channel_ids))
        ).all()
        rows_by_external_id = {row.youtube_channel_id: row for row in rows}
        missing = [channel_id for channel_id in unique_channel_ids if channel_id not in rows_by_external_id]
        if missing:
            raise KeyError(f"Channel not found: {missing[0]}")
        return [rows_by_external_id[channel_id] for channel_id in unique_channel_ids]

    def _to_entry(self, row: ChannelGroupORM) -> ChannelGroupEntry:
        channel_ids = self._session.scalars(
            select(YouTubeChannelORM.youtube_channel_id)
            .join(ChannelGroupMemberORM, ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id)
            .where(ChannelGroupMemberORM.group_id == row.id)
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return ChannelGroupEntry(
            id=str(row.id),
            name=row.name,
            group_type=row.group_type,
            active=row.active,
            channel_ids=tuple(channel_ids),
        )
