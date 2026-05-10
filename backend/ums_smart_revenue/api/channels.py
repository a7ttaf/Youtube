from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditRecord, AuditSink, InMemoryAuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryConflictError,
    ChannelRegistryStore,
    ChannelRegistryValidationError,
    bootstrap_channel_registry,
)
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

    @field_validator("youtube_channel_id", "channel_name", "primary_company_id", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class ChannelMappingRequest(BaseModel):
    primary_company_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("primary_company_id", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


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
    authorized_channel_ids = _authorized_channel_ids_for_analytics(user, org_index)
    if authorized_channel_ids is None:
        visible_channels = registry.list_channels()
    else:
        visible_channels = registry.list_channels_by_ids(authorized_channel_ids)
        visible_channels = [
            channel
            for channel in visible_channels
            if has_permission(user, Permission.VIEW_ANALYTICS, AccessScope.channel(channel.youtube_channel_id), org_index)
        ]
    return [channel.to_api() for channel in visible_channels]


def _authorized_channel_ids_for_analytics(user: UserPrincipal, org_index: OrgAccessIndex) -> set[str] | None:
    if user.disabled:
        return set()

    channel_ids: set[str] = set()
    for scope in _granted_scopes_for_permission(user, Permission.VIEW_ANALYTICS):
        if scope.type == ScopeType.GLOBAL:
            return None
        if scope.type == ScopeType.CHANNEL and scope.id is not None:
            channel_ids.add(scope.id)
        elif scope.type == ScopeType.COMPANY and scope.id is not None:
            channel_ids.update(
                channel_id for channel_id, company_id in org_index.channel_company.items() if company_id == scope.id
            )
        elif scope.type == ScopeType.SECTOR and scope.id is not None:
            channel_ids.update(
                channel_id for channel_id, sector_id in org_index.channel_sector.items() if sector_id == scope.id
            )
    return channel_ids


def _granted_scopes_for_permission(user: UserPrincipal, permission: Permission) -> tuple[AccessScope, ...]:
    scopes: list[AccessScope] = []
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            scopes.append(grant.scope)
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset()):
            scopes.append(assignment.scope)
    return tuple(scopes)


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
    except ChannelRegistryValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ChannelRegistryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Channel already exists") from exc
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
    current_scope = AccessScope.channel(youtube_channel_id)
    target_scope = AccessScope.company(payload.primary_company_id)
    can_manage_current = has_permission(user, Permission.MANAGE_ORG_MAPPING, current_scope, org_index)
    can_manage_target = has_permission(user, Permission.MANAGE_ORG_MAPPING, target_scope, org_index)
    if not (can_manage_current and can_manage_target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_ORG_MAPPING.value}",
        )

    current_channel = registry.get_channel(youtube_channel_id)
    if current_channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    try:
        updated = registry.update_mapping(
            youtube_channel_id=youtube_channel_id,
            primary_company_id=payload.primary_company_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
    except ChannelRegistryValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ChannelRegistryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Channel already exists") from exc
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
