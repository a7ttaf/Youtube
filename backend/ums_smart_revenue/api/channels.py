from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditRecord, AuditSink, InMemoryAuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryStore, bootstrap_channel_registry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry


router = APIRouter(prefix="/channels", tags=["channels"])

_CHANNEL_REGISTRY = bootstrap_channel_registry()
_AUDIT_SINK = InMemoryAuditSink()


class ChannelCreateRequest(BaseModel):
    youtube_channel_id: str = Field(min_length=1)
    channel_name: str = Field(min_length=1)
    primary_company_id: str = Field(min_length=1)
    cms_status: str
    revenue_required: bool


class ChannelMappingRequest(BaseModel):
    primary_company_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def current_channel_registry() -> ChannelRegistry:
    return _CHANNEL_REGISTRY


def sql_channel_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelRegistry:
    return SqlAlchemyChannelRegistry(session)


def current_audit_sink() -> InMemoryAuditSink:
    return _AUDIT_SINK


def sql_audit_sink_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAuditSink:
    return SqlAlchemyAuditSink(session)


def audit_record_to_api(record: AuditRecord) -> dict[str, object]:
    return {
        "event_type": record.event_type,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "reason": record.reason,
        "sensitive": record.sensitive,
    }


@router.get("")
def list_channels(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> list[dict[str, object]]:
    visible_channels = [
        channel.to_api()
        for channel in registry.list_channels()
        if has_permission(user, Permission.VIEW_ANALYTICS, AccessScope.channel(channel.youtube_channel_id), org_index)
    ]
    return visible_channels


@router.post("", status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> dict[str, object]:
    target_scope = AccessScope.company(payload.primary_company_id)
    if not has_permission(user, Permission.MANAGE_CHANNELS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_CHANNELS.value}",
        )
    try:
        channel = registry.create_channel(
            youtube_channel_id=payload.youtube_channel_id,
            channel_name=payload.channel_name,
            primary_company_id=payload.primary_company_id,
            cms_status=payload.cms_status,
            revenue_required=payload.revenue_required,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return channel.to_api()


@router.patch("/{youtube_channel_id}/mapping")
def update_channel_mapping(
    youtube_channel_id: str,
    payload: ChannelMappingRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    current_channel = registry.get_channel(youtube_channel_id)
    if current_channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    current_scope = AccessScope.channel(youtube_channel_id)
    target_scope = AccessScope.company(payload.primary_company_id)
    can_manage_current = has_permission(user, Permission.MANAGE_ORG_MAPPING, current_scope, org_index)
    can_manage_target = has_permission(user, Permission.MANAGE_ORG_MAPPING, target_scope, org_index)
    if not (can_manage_current and can_manage_target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_ORG_MAPPING.value}",
        )

    updated = registry.update_mapping(
        youtube_channel_id=youtube_channel_id,
        primary_company_id=payload.primary_company_id,
    )
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id=youtube_channel_id,
        scope=target_scope,
        reason=payload.reason,
        details={
            "old_primary_company_id": current_channel.primary_company_id,
            "new_primary_company_id": payload.primary_company_id,
        },
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
