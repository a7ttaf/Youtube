from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupConflictError, ChannelGroupEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_SQLITE_DUPLICATE_GROUP_MEMBER_ERROR = (
    "unique constraint failed: channel_group_members.group_id, channel_group_members.channel_id"
)
_SQLITE_DUPLICATE_CMS_GROUP_ERROR = (
    "unique constraint failed: channel_groups.tenant_id, channel_groups.cms_group_id"
)


class SqlAlchemyChannelGroupRegistry:
    """SQL-backed channel group registry scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def list_groups(self) -> list[ChannelGroupEntry]:
        """Return active channel groups with their member channel ids.

        Member channels are filtered to active rows so the revenue scope
        selector operates on the same set the revenue read path uses
        (via get_active_member_channels). Management callers that need
        the full member set should call list_groups_full instead.
        """
        rows = self._session.scalars(
            select(ChannelGroupORM)
            .where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.active.is_(True),
            )
            .order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group_active(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ())) for row in rows
        ]

    def list_groups_full(self) -> list[ChannelGroupEntry]:
        """Return active channel groups with their FULL member channel ids.

        Distinct from list_groups so the groups management API can
        authorize over the complete member set (including channels that
        have since been deactivated and are outside the caller's org
        scope), while the scope selector continues to operate on the
        active-only member set.
        """
        rows = self._session.scalars(
            select(ChannelGroupORM)
            .where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.active.is_(True),
            )
            .order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ())) for row in rows
        ]

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        row = self._get_group_row(group_id)
        if row is None:
            return None
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Resolve the channel group carrying one YouTube CMS group key —
    #   the lookup the bulk channel import reconciles Group_ID columns against
    #   (create the group when absent, attach membership when present).
    # Database/ORM: ChannelGroupORM (read-only), tenant-scoped; uniqueness is
    #   enforced by unique (tenant_id, cms_group_id) from 20260803_0001.
    # Standards: Returns archived groups too (no active filter) so the import
    #   PLANNER can fail rows targeting a retired group closed instead of
    #   mutating it; callers must check .active before writing membership.
    # Blast Radius: Channel-group membership and therefore finance group-scope
    #   selection. No finance totals, no allocation.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores (archived-group planning gate via
    #     list_archived_cms_group_ids) and membership attachment for
    #     non-archived groups (write-boundary for_update recheck here).
    # ========================================================================
    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Return the tenant-scoped group carrying this CMS key, or None.

        ``for_update`` row-locks the group so a write-boundary active-state
        check cannot race a concurrent archive (SQLite ignores FOR UPDATE).
        """
        statement = select(ChannelGroupORM).where(
            ChannelGroupORM.tenant_id == self._tenant_id,
            ChannelGroupORM.cms_group_id == cms_group_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalars(statement).one_or_none()
        if row is None:
            return None
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Bulk-classify a roster's CMS group keys by archived state so
    #   import planning can fail rows targeting archived groups closed without
    #   a lookup-per-group query storm.
    # Database/ORM: ChannelGroupORM (read-only; cms_group_id column under the
    #   per-tenant unique key). No membership loading, no writes.
    # Standards: One bounded SELECT for the whole key set; tenant-scoped;
    #   unknown keys are simply absent from the result (planning treats them
    #   as creatable). Read-only -> RLS-safe, no platform lane.
    # Blast Radius: Import planning's archived-group per-row errors, which
    #   gate finance-scope group mutations. No group writes, no audit.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores feeds the result to the planner.
    #   - File: backend/ums_smart_revenue/org/channel_import.py ->
    #     plan_channel_import fails rows whose key lands in this set.
    # ========================================================================
    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is archived.

        One bounded SELECT (no per-key round trips, no membership loading) so
        import planning can vet a full 5000-row roster's group keys without a
        lookup-per-group query storm.
        """
        if not cms_group_ids:
            return set()
        rows = self._session.scalars(
            select(ChannelGroupORM.cms_group_id).where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id.in_(cms_group_ids),
                ChannelGroupORM.active.is_(False),
            )
        ).all()
        # The IN clause already excludes NULL keys; the comprehension narrows
        # the column's Optional type for the checker without a cast.
        return {key for key in rows if key is not None}

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        """Return active member channel ids for a group, or None if the group is missing.

        Distinct from get_group (which returns ALL members for management authz)
        so the revenue read path operates on the same set the scope selector
        advertises, while the groups management API can still authorize over
        the full member set (including channels that have since been
        deactivated and are outside the caller's org scope).
        """
        row = self._get_group_row(group_id)
        if row is None:
            return None
        rows = self._session.execute(
            select(YouTubeChannelORM.youtube_channel_id)
            .join(
                ChannelGroupMemberORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == row.id,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return tuple(row.youtube_channel_id for row in rows)

    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
    ) -> ChannelGroupEntry:
        """Create a group row plus membership, raising typed on a CMS-key race.

        On ``ChannelGroupConflictError`` the underlying IntegrityError has
        already aborted the Postgres transaction: the session is unusable
        until rolled back, so callers must fail the request (as the import
        and groups routes do), never catch-and-retry on the same session.
        """
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        row = ChannelGroupORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            name=name,
            group_type=group_type,
            active=True,
            cms_group_id=cms_group_id,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # Two concurrent imports can both see a CMS key as missing and race
            # this INSERT; the per-tenant unique key makes the loser fail here.
            # Translate to the typed conflict (route: 409, retryable) instead
            # of letting the IntegrityError escape as a 500.
            if not _is_duplicate_cms_group_integrity_error(exc):
                raise
            raise ChannelGroupConflictError(
                f"channel group already exists for cms_group_id: {cms_group_id}"
            ) from exc
        self._replace_member_rows(row.id, channel_rows)
        self._session.flush()
        return self._to_entry(row)

    def update_group(
        self, *, group_id: str, name: str | None, active: bool | None
    ) -> ChannelGroupEntry:
        row = self._require_group_row(group_id)
        if name is not None:
            row.name = name
        if active is not None:
            row.active = active
        self._session.flush()
        return self._to_entry(row)

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        # The parent group row is the membership serialization point: every
        # membership writer locks it, so a reader that checked membership
        # under the same lock (the import's get_group_by_cms_id
        # for_update=True) cannot have its skip-decision invalidated by a
        # concurrent add/remove committing mid-request (review #159
        # r3713841264). SQLite ignores FOR UPDATE (single-writer anyway).
        row = self._require_group_row(group_id, for_update=True)
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        existing_ids = set(
            self._session.scalars(
                select(ChannelGroupMemberORM.channel_id).where(
                    ChannelGroupMemberORM.tenant_id == self._tenant_id,
                    ChannelGroupMemberORM.group_id == row.id,
                )
            ).all()
        )
        pending_channel_ids = [
            channel.id for channel in channel_rows if channel.id not in existing_ids
        ]
        if not pending_channel_ids:
            return self._to_entry(row)

        nested = self._session.begin_nested()
        try:
            for channel_id in pending_channel_ids:
                self._session.add(
                    ChannelGroupMemberORM(
                        tenant_id=self._tenant_id,
                        group_id=row.id,
                        channel_id=channel_id,
                    )
                )
            self._session.flush()
        except IntegrityError as exc:
            nested.rollback()
            if not _is_duplicate_group_member_integrity_error(exc):
                raise
            row = self._require_group_row(group_id)
            existing_ids = set(
                self._session.scalars(
                    select(ChannelGroupMemberORM.channel_id).where(
                        ChannelGroupMemberORM.tenant_id == self._tenant_id,
                        ChannelGroupMemberORM.group_id == row.id,
                    )
                ).all()
            )
            for channel_id in pending_channel_ids:
                if channel_id not in existing_ids:
                    self._session.add(
                        ChannelGroupMemberORM(
                            tenant_id=self._tenant_id,
                            group_id=row.id,
                            channel_id=channel_id,
                        )
                    )
            self._session.flush()
        else:
            nested.commit()
        return self._to_entry(row)

    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        # Same membership serialization point as add_members: without the
        # parent-row lock a concurrent import could read the channel as a
        # member (under ITS lock), skip the add, and then let this delete
        # commit — a successful import whose requested membership is absent.
        row = self._require_group_row(group_id, for_update=True)
        channel = self._channel_rows_by_external_ids([channel_id])[0]
        self._session.execute(
            delete(ChannelGroupMemberORM).where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == row.id,
                ChannelGroupMemberORM.channel_id == channel.id,
            )
        )
        self._session.flush()
        return self._to_entry(row)

    def _replace_member_rows(self, group_id: UUID, channel_rows: list[YouTubeChannelORM]) -> None:
        self._session.execute(
            delete(ChannelGroupMemberORM).where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == group_id,
            )
        )
        for channel in channel_rows:
            self._session.add(
                ChannelGroupMemberORM(
                    tenant_id=self._tenant_id,
                    group_id=group_id,
                    channel_id=channel.id,
                )
            )

    def _get_group_row(self, group_id: str, *, for_update: bool = False) -> ChannelGroupORM | None:
        try:
            group_uuid = UUID(group_id)
        except (TypeError, ValueError) as _:
            return None
        statement = select(ChannelGroupORM).where(
            ChannelGroupORM.tenant_id == self._tenant_id,
            ChannelGroupORM.id == group_uuid,
        )
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def _require_group_row(self, group_id: str, *, for_update: bool = False) -> ChannelGroupORM:
        row = self._get_group_row(group_id, for_update=for_update)
        if row is None:
            raise KeyError(f"Group not found: {group_id}")
        return row

    def _channel_rows_by_external_ids(self, channel_ids: list[str]) -> list[YouTubeChannelORM]:
        unique_channel_ids = list(dict.fromkeys(channel_ids))
        if not unique_channel_ids:
            return []
        rows = self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id.in_(unique_channel_ids),
            )
        ).all()
        rows_by_external_id = {row.youtube_channel_id: row for row in rows}
        missing = [
            channel_id for channel_id in unique_channel_ids if channel_id not in rows_by_external_id
        ]
        if missing:
            raise KeyError(f"Channel not found: {missing[0]}")
        return [rows_by_external_id[channel_id] for channel_id in unique_channel_ids]

    def _channel_ids_by_group(self, group_ids: list[UUID]) -> dict[UUID, tuple[str, ...]]:
        if not group_ids:
            return {}
        rows = self._session.execute(
            select(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
            .join(
                YouTubeChannelORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id.in_(group_ids),
            )
            .order_by(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
        ).all()
        channel_ids_by_group: dict[UUID, list[str]] = {}
        for group_id, youtube_channel_id in rows:
            channel_ids_by_group.setdefault(group_id, []).append(youtube_channel_id)
        return {
            group_id: tuple(channel_ids) for group_id, channel_ids in channel_ids_by_group.items()
        }

    def _channel_ids_by_group_active(self, group_ids: list[UUID]) -> dict[UUID, tuple[str, ...]]:
        """Same shape as _channel_ids_by_group but excludes inactive channels.

        Used by list_groups (the scope selector) so the selector advertises
        the same member set the revenue read path will filter to. The
        full-member set is still available via _channel_ids_by_group for
        management authz (list_groups_full).
        """
        if not group_ids:
            return {}
        rows = self._session.execute(
            select(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
            .join(
                YouTubeChannelORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id.in_(group_ids),
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
        ).all()
        channel_ids_by_group: dict[UUID, list[str]] = {}
        for group_id, youtube_channel_id in rows:
            channel_ids_by_group.setdefault(group_id, []).append(youtube_channel_id)
        return {
            group_id: tuple(channel_ids) for group_id, channel_ids in channel_ids_by_group.items()
        }

    def _to_entry(
        self, row: ChannelGroupORM, *, channel_ids: tuple[str, ...] | None = None
    ) -> ChannelGroupEntry:
        resolved_channel_ids = (
            channel_ids
            if channel_ids is not None
            else tuple(
                self._session.scalars(
                    select(YouTubeChannelORM.youtube_channel_id)
                    .join(
                        ChannelGroupMemberORM,
                        (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                        & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
                    )
                    .where(
                        ChannelGroupMemberORM.tenant_id == self._tenant_id,
                        ChannelGroupMemberORM.group_id == row.id,
                    )
                    .order_by(YouTubeChannelORM.youtube_channel_id)
                ).all()
            )
        )
        return ChannelGroupEntry(
            id=str(row.id),
            name=row.name,
            group_type=row.group_type,
            active=row.active,
            channel_ids=resolved_channel_ids,
            cms_group_id=row.cms_group_id,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("tenant_id must be a valid UUID") from exc


def _is_duplicate_group_member_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").lower()
    error_text = f"{exc.orig!s} {exc!s}".lower()
    return (
        "channel_group_members_pkey" in constraint_name
        or _SQLITE_DUPLICATE_GROUP_MEMBER_ERROR in error_text
    )


def _is_duplicate_cms_group_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").lower()
    error_text = f"{exc.orig!s} {exc!s}".lower()
    return (
        "uq_channel_groups_tenant_id_cms_group_id" in constraint_name
        or "uq_channel_groups_tenant_id_cms_group_id" in error_text
        or _SQLITE_DUPLICATE_CMS_GROUP_ERROR in error_text
    )
