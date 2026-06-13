from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import (
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
    record_audit_event,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.org.channel_issues import (
    build_channel_registry_issues,
    summarize_channel_registry_issues,
)
from ums_smart_revenue.org.channel_registry import (
    ChannelMappingLockedMonthError,
    ChannelRegistry,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryStore,
    ChannelRegistryValidationError,
    bootstrap_channel_registry,
)
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry

router = APIRouter(prefix="/channels", tags=["channels"])

_CHANNEL_REGISTRY = bootstrap_channel_registry()
_AUDIT_SINK = InMemoryAuditSink()
_OFFICIAL_REVENUE_SOURCE_STATUSES = frozenset(
    {"OFFICIAL_CMS_REVENUE", "OFFICIAL_MANUAL_IMPORT"}
)


class ChannelCreateRequest(BaseModel):
    youtube_channel_id: str = Field(min_length=1)
    channel_name: str = Field(min_length=1)
    primary_company_id: str = Field(min_length=1)
    cms_status: str
    revenue_required: bool

    @field_validator(
        "youtube_channel_id", "channel_name", "primary_company_id", mode="before"
    )
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
    session: Annotated[Session, Depends(current_platform_db_session)],
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
    _require_analytics_view_permission(user)
    return [
        channel.to_api()
        for channel in _visible_channels_for_analytics(
            user=user,
            registry=registry,
            org_index=org_index,
        )
    ]


@router.get("/outside-cms")
def list_outside_cms_channels(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> dict[str, object]:
    _require_analytics_view_permission(user)
    channels = [
        channel
        for channel in _visible_channels_for_analytics(
            user=user,
            registry=registry,
            org_index=org_index,
        )
        if channel.cms_status == "OUTSIDE_CMS"
    ]
    items = [_outside_cms_channel_to_api(channel) for channel in channels]
    return {
        "items": items,
        "summary": {
            "outside_cms_channel_count": len(items),
            "revenue_required_count": sum(
                1 for item in items if item["revenue_required"]
            ),
            "missing_official_revenue_count": sum(
                1 for item in items if item["missing_official_revenue"]
            ),
        },
    }


@router.get("/issues")
def list_channel_issues(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> dict[str, object]:
    _require_analytics_view_permission(user)
    issues = build_channel_registry_issues(
        channels=_visible_channels_for_analytics(
            user=user,
            registry=registry,
            org_index=org_index,
        ),
        groups=group_registry.list_groups(),
        org_index=org_index,
    )
    return {
        "items": [issue.to_api() for issue in issues],
        "summary": summarize_channel_registry_issues(issues),
    }


def _require_analytics_view_permission(user: UserPrincipal) -> None:
    # Explicit 403 instead of returning a silent empty result: analytics
    # consumers without VIEW_ANALYTICS should fail authorization, not see
    # an empty channel feed that could be mistaken for "no channels exist".
    if user.disabled or not _granted_scopes_for_permission(
        user, Permission.VIEW_ANALYTICS
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_ANALYTICS.value}",
        )


def _visible_channels_for_analytics(
    *,
    user: UserPrincipal,
    registry: ChannelRegistryStore,
    org_index: OrgAccessIndex,
) -> list[ChannelRegistryEntry]:
    authorized_channel_ids = _authorized_channel_ids_for_analytics(user, org_index)
    if authorized_channel_ids is None:
        return registry.list_channels()
    visible_channels = registry.list_channels_by_ids(authorized_channel_ids)
    # Defense-in-depth: re-evaluate per-channel permission even though
    # authorized_channel_ids was derived from the same org_index.  This guards
    # against a registry returning IDs outside the computed set (e.g. a buggy
    # list_channels_by_ids implementation) and keeps the access path fail-closed.
    return [
        channel
        for channel in visible_channels
        if has_permission(
            user,
            Permission.VIEW_ANALYTICS,
            AccessScope.channel(channel.youtube_channel_id),
            org_index,
        )
    ]


def _outside_cms_channel_to_api(channel: ChannelRegistryEntry) -> dict[str, object]:
    missing_official_revenue = (
        channel.revenue_required
        and channel.revenue_source_status not in _OFFICIAL_REVENUE_SOURCE_STATUSES
    )
    return {
        "youtube_channel_id": channel.youtube_channel_id,
        "channel_name": channel.channel_name,
        "primary_company_id": channel.primary_company_id,
        "cms_status": channel.cms_status,
        "content_owner_id": channel.content_owner_id,
        "revenue_required": channel.revenue_required,
        "revenue_source_status": channel.revenue_source_status,
        "missing_official_revenue": missing_official_revenue,
        "recommended_action": _recommended_outside_cms_action(
            revenue_required=channel.revenue_required,
            revenue_source_status=channel.revenue_source_status,
            missing_official_revenue=missing_official_revenue,
        ),
    }


def _recommended_outside_cms_action(
    *,
    revenue_required: bool,
    revenue_source_status: str,
    missing_official_revenue: bool,
) -> str:
    if not revenue_required or revenue_source_status == "PERFORMANCE_ONLY":
        return "Confirm performance-only classification."
    if missing_official_revenue:
        return "Link channel to CMS or import official manual revenue."
    if revenue_source_status == "OFFICIAL_MANUAL_IMPORT":
        return (
            "Keep manual official revenue import current; "
            "CMS linking remains recommended."
        )
    return "Verify CMS link and continue normal ingestion."


def _authorized_channel_ids_for_analytics(
    user: UserPrincipal, org_index: OrgAccessIndex
) -> set[str] | None:
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
                channel_id
                for channel_id, company_id in org_index.channel_company.items()
                if company_id == scope.id
            )
        elif scope.type == ScopeType.SECTOR and scope.id is not None:
            channel_ids.update(
                channel_id
                for channel_id, sector_id in org_index.channel_sector.items()
                if sector_id == scope.id
            )
    return channel_ids


def _granted_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> tuple[AccessScope, ...]:
    scopes: list[AccessScope] = []
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            scopes.append(grant.scope)
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(
            assignment.role, frozenset()
        ):
            scopes.append(assignment.scope)
    return tuple(scopes)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ChannelRegistryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Channel already exists"
        ) from exc
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.CHANNEL_CREATED,
        entity_type="youtube_channel",
        entity_id=channel.youtube_channel_id,
        scope=target_scope,
        details={
            "channel_name": channel.channel_name,
            "primary_company_id": channel.primary_company_id,
            "cms_status": channel.cms_status,
            "revenue_required": channel.revenue_required,
        },
    )
    response = channel.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


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
    can_manage_current = has_permission(
        user, Permission.MANAGE_ORG_MAPPING, current_scope, org_index
    )
    can_manage_target = has_permission(
        user, Permission.MANAGE_ORG_MAPPING, target_scope, org_index
    )
    if not (can_manage_current and can_manage_target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_ORG_MAPPING.value}",
        )

    current_channel = registry.get_channel(youtube_channel_id)
    if current_channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        )

    # FIX: Detect a no-op PATCH at the route boundary so the audit decision
    # (suppress CHANNEL_UPDATED) lives with actor + reason, not the registry.
    # The registry short-circuits the locked-month guard for the same case
    # (no re-parenting occurs), so the returned entry still reflects the
    # existing mapping — we must not audit a non-change (review #98 T1:
    # idempotent retries should not produce false audit events).
    is_no_op = current_channel.primary_company_id == payload.primary_company_id

    try:
        updated = registry.update_mapping(
            youtube_channel_id=youtube_channel_id,
            primary_company_id=payload.primary_company_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        ) from exc
    # Placed before the audit write below: a mapping change rejected because the
    # channel has facts in a LOCKED finance month must NOT be recorded as an
    # applied CHANNEL_UPDATED event.
    except ChannelMappingLockedMonthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except ChannelRegistryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ChannelRegistryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Channel already exists"
        ) from exc
    response = updated.to_api()
    if is_no_op:
        response["audit_event"] = None
        return response
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
    response["audit_event"] = audit_record_to_api(record)
    return response
