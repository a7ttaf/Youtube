from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry, ChannelGroupRegistryStore
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry


router = APIRouter(prefix="/groups", tags=["groups"])

_GROUP_REGISTRY = ChannelGroupRegistry()

GROUP_TYPES = frozenset(
    {
        "HOLDING",
        "SECTOR",
        "COMPANY",
        "TV_BRAND",
        "NEWS_BRAND",
        "CUSTOM_GROUP",
        "FINANCE_GROUP",
        "SEASONAL_GROUP",
    }
)


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    group_type: str = Field(min_length=1)
    channel_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    active: bool | None = None
    reason: str = Field(min_length=1)


class GroupMembersRequest(BaseModel):
    channel_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


def current_group_registry() -> ChannelGroupRegistry:
    return _GROUP_REGISTRY


def sql_group_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelGroupRegistry:
    return SqlAlchemyChannelGroupRegistry(session)


@router.get("")
def list_groups(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> list[dict[str, object]]:
    return [
        group.to_api()
        for group in registry.list_groups()
        if _can_view_group(user=user, group=group, org_index=org_index)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_group(
    payload: GroupCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_group_type(payload.group_type)
    _require_manage_group_channels(user=user, channel_ids=payload.channel_ids, org_index=org_index)
    group = registry.create_group(
        name=payload.name,
        group_type=payload.group_type,
        channel_ids=payload.channel_ids,
    )
    record = _audit_group_change(
        audit_sink=audit_sink,
        user=user,
        group=group,
        reason=payload.reason,
        action="created",
    )
    response = group.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.patch("/{group_id}")
def update_group(
    group_id: str,
    payload: GroupUpdateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    group = _require_group(registry, group_id)
    _require_manage_group_channels(user=user, channel_ids=list(group.channel_ids), org_index=org_index)
    updated = registry.update_group(group_id=group_id, name=payload.name, active=payload.active)
    record = _audit_group_change(
        audit_sink=audit_sink,
        user=user,
        group=updated,
        reason=payload.reason,
        action="updated",
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/{group_id}/members")
def add_group_members(
    group_id: str,
    payload: GroupMembersRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    group = _require_group(registry, group_id)
    channel_ids = list(dict.fromkeys([*group.channel_ids, *payload.channel_ids]))
    _require_manage_group_channels(user=user, channel_ids=channel_ids, org_index=org_index)
    updated = registry.add_members(group_id=group_id, channel_ids=payload.channel_ids)
    record = _audit_group_change(
        audit_sink=audit_sink,
        user=user,
        group=updated,
        reason=payload.reason,
        action="members_added",
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.delete("/{group_id}/members/{channel_id}")
def remove_group_member(
    group_id: str,
    channel_id: str,
    reason: Annotated[str, Query(min_length=1)],
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    group = _require_group(registry, group_id)
    _require_manage_group_channels(user=user, channel_ids=list(group.channel_ids), org_index=org_index)
    updated = registry.remove_member(group_id=group_id, channel_id=channel_id)
    record = _audit_group_change(
        audit_sink=audit_sink,
        user=user,
        group=updated,
        reason=reason,
        action="member_removed",
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _validate_group_type(group_type: str) -> None:
    if group_type not in GROUP_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Unknown group type: {group_type}")


def _can_view_group(*, user: UserPrincipal, group: ChannelGroupEntry, org_index: OrgAccessIndex) -> bool:
    if not group.channel_ids:
        return has_permission(user, Permission.VIEW_ANALYTICS, AccessScope.global_scope(), org_index)
    return all(
        has_permission(user, Permission.VIEW_ANALYTICS, AccessScope.channel(channel_id), org_index)
        for channel_id in group.channel_ids
    )


def _require_manage_group_channels(*, user: UserPrincipal, channel_ids: list[str], org_index: OrgAccessIndex) -> None:
    unique_channel_ids = list(dict.fromkeys(channel_ids))
    allowed = (
        has_permission(user, Permission.MANAGE_GROUPS, AccessScope.global_scope(), org_index)
        if not unique_channel_ids
        else all(
            has_permission(user, Permission.MANAGE_GROUPS, AccessScope.channel(channel_id), org_index)
            for channel_id in unique_channel_ids
        )
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_GROUPS.value}",
        )


def _require_group(registry: ChannelGroupRegistryStore, group_id: str) -> ChannelGroupEntry:
    group = registry.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


def _audit_group_change(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    group: ChannelGroupEntry,
    reason: str,
    action: str,
):
    return record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.GROUP_UPDATED,
        entity_type="channel_group",
        entity_id=group.id,
        scope=AccessScope.global_scope(),
        reason=reason,
        details={"action": action, "group_type": group.group_type, "channel_ids": list(group.channel_ids)},
    )
