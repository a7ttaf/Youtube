# ============================================================================
# Purpose: Channel HTTP routes — registry listing/creation, mapping and
#   content-owner updates, registry health feeds, and the bulk channel
#   inventory import (POST /channels/import).
# Database/ORM: YouTubeChannelORM via ChannelRegistryStore dependencies;
#   ChannelGroupORM via the group registry; AuditLogORM via audit sinks.
# Standards: Thin routes — authorize, validate, translate typed domain errors
#   to HTTP, and delegate business logic to org/ domain modules
#   (channel_import parse/plan, channel_import_apply execution). Fail closed
#   on permissions; audit every applied write.
# Blast Radius: Channel registry surface, connector ingest targeting (via
#   cms_status/content_owner_id), audit trail. No finance totals.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> parse/plan core.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> apply+audit.
#   - File: backend/ums_smart_revenue/app.py -> dependency wiring.
# ============================================================================
"""Channel registry and bulk-import HTTP routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.dependencies_finance import current_org_access_index
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
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
from ums_smart_revenue.auth.sql_audit_sink import PlatformLaneAuditSink, SqlAlchemyAuditSink
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.org.channel_import import (
    ChannelImportFormatError,
    ChannelImportPlan,
    ParsedChannelImport,
    parse_channel_import_csv,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_import_apply import apply_channel_import
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
    ChannelRevenueRequirementLockedMonthError,
    bootstrap_channel_registry,
)
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry

router = APIRouter(prefix="/channels", tags=["channels"])

_CHANNEL_REGISTRY = bootstrap_channel_registry()
_AUDIT_SINK = InMemoryAuditSink()
_OFFICIAL_REVENUE_SOURCE_STATUSES = frozenset({"OFFICIAL_CMS_REVENUE", "OFFICIAL_MANUAL_IMPORT"})

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
# Mirrors the youtube_channels cms_status CHECK in 20260510_0002_org_registry.
IMPORTABLE_CMS_STATUSES = frozenset({"INSIDE_CMS", "OUTSIDE_CMS", "UNKNOWN"})


class ChannelCreateRequest(BaseModel):
    youtube_channel_id: str = Field(min_length=1)
    channel_name: str = Field(min_length=1)
    primary_company_id: str = Field(min_length=1)
    cms_status: str
    revenue_required: bool
    content_owner_id: str | None = None

    @field_validator("youtube_channel_id", "channel_name", "primary_company_id", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)

    @field_validator("content_owner_id", mode="before")
    @classmethod
    def strip_optional_content_owner(cls, value):
        return _strip_optional_string(value)


class ChannelMappingRequest(BaseModel):
    primary_company_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("primary_company_id", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class ContentOwnerUpdateRequest(BaseModel):
    # content_owner_id is required-to-be-present but nullable: sending null
    # clears the CMS content owner; a present-but-blank string is rejected.
    content_owner_id: str | None
    reason: str = Field(min_length=1)

    @field_validator("content_owner_id", mode="before")
    @classmethod
    def strip_optional_content_owner(cls, value):
        return _strip_optional_string(value)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class ChannelImportFieldChange(BaseModel):
    """One field's before/after pair in an import row's diff."""

    model_config = ConfigDict(populate_by_name=True)

    from_value: str | bool | None = Field(alias="from")
    to_value: str | bool | None = Field(alias="to")


class ChannelImportRowResult(BaseModel):
    """One CSV row's planned or applied outcome."""

    row_number: int
    youtube_channel_id: str | None
    outcome: str
    channel_name: str | None
    group_id: str | None
    revenue_required: bool | None
    changes: dict[str, ChannelImportFieldChange]
    reason: str | None


class ChannelImportResult(BaseModel):
    """Declared response contract for POST /channels/import (dry run and apply)."""

    dry_run: bool
    content_owner_id: str
    cms_status: str
    counts: dict[str, int]
    rows: list[ChannelImportRowResult]


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


def _strip_optional_string(value: str | None) -> str | None:
    # None stays None (field unset); a present string is stripped and a
    # blank-after-strip value is rejected rather than silently coerced to null.
    if value is None:
        return None
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


def current_import_audit_sink(
    sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AuditSink:
    """Audit sink for the bulk import; passes through until SQL wiring overrides it.

    create_app overrides this with sql_import_audit_sink_from_session so import
    audit rows join the tenant transaction (all-or-nothing with the channel
    writes) instead of committing on the independent platform session.
    """
    return sink


def sql_import_audit_sink_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> PlatformLaneAuditSink:
    """Bind the import's audit writes to the request's tenant session.

    The import promises all-or-nothing semantics. The app-wide audit sink runs
    on the independently committed platform session, which FastAPI tears down
    (and commits) BEFORE the tenant session; a tenant commit failure would then
    leave audit rows permanently claiming an import that never happened.
    PlatformLaneAuditSink writes audit_logs inside the SAME transaction as the
    channel writes, elevating per append because audit_logs is platform-only
    writable.
    """
    return PlatformLaneAuditSink(session)


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
            "revenue_required_count": sum(1 for item in items if item["revenue_required"]),
            "missing_official_revenue_count": sum(
                1 for item in items if item["missing_official_revenue"]
            ),
        },
    }


@router.get("/issues")
def list_channel_issues(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
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
    if user.disabled or not _granted_scopes_for_permission(user, Permission.VIEW_ANALYTICS):
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
        return "Keep manual official revenue import current; CMS linking remains recommended."
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


def _direct_scopes_for_permission(user: UserPrincipal, permission: Permission) -> list[AccessScope]:
    return [
        grant.scope
        for grant in user.direct_permissions
        if grant.active and grant.permission == permission
    ]


def _role_scopes_for_permission(user: UserPrincipal, permission: Permission) -> list[AccessScope]:
    return [
        assignment.scope
        for assignment in user.role_assignments
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset())
    ]


def _granted_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> tuple[AccessScope, ...]:
    return tuple(
        _direct_scopes_for_permission(user, permission)
        + _role_scopes_for_permission(user, permission)
    )


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
            content_owner_id=payload.content_owner_id,
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
            "content_owner_id": channel.content_owner_id,
        },
    )
    response = channel.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


# ============================================================================
# Purpose: Load an operator's CMS channel roster in bulk. One CSV upload
#   creates missing channels and refreshes the inventory fields (name,
#   cms_status, content_owner_id, revenue_required) of existing ones, with a
#   dry run that shows the exact diff before anything is written.
# Database/ORM: YouTubeChannelORM via ChannelRegistryStore (create_channel /
#   update_inventory) and ChannelGroupORM + membership via
#   ChannelGroupRegistryStore. No finance tables are touched.
# Standards: Fail closed on global MANAGE_CHANNELS — a roster file is not
#   scoped to one company, so a company-scoped manager must not run it. A
#   roster carrying Group_ID values additionally requires global MANAGE_GROUPS:
#   the two permissions are independently grantable and group mutations must
#   not bypass the group API's checks. Errors are reported per row for the
#   whole file (an operator fixes one file, not one row at a time), but the
#   apply is all-or-nothing: a single ERROR row rejects the request before any
#   write, and audit rows are written through the SAME tenant transaction as
#   the channel writes (platform-lane elevation per append), so a failed
#   commit can never leave audit rows describing an import that did not
#   happen. A dry run writes nothing at all, including no audit event, so
#   previewing is never mistaken for applying. Every write is audited per
#   channel (permission_override=MANAGE_CHANNELS — the permission that
#   actually authorized it), every group creation/membership addition gets its
#   own GROUP_UPDATED record, plus one CHANNEL_IMPORTED summary; because
#   CHANNEL_UPDATED declares reason_required the route takes a mandatory
#   reason rather than letting an upsert 500 mid-apply. Group membership is
#   reconciled for CREATE, UPDATE, and UNCHANGED rows alike: the outcome is
#   computed only from inventory fields, so treating UNCHANGED as a no-op
#   would silently drop a newly added Group_ID on re-import.
# Blast Radius: Connector ingest targeting. list_target_channels only pulls
#   revenue for cms_status='INSIDE_CMS' channels, so importing a roster with
#   the wrong cms_status silently removes channels from ingest and their
#   revenue simply stops arriving with no error — the dry-run diff is the
#   operator's guard against that. No month locks, no allocation, no exports.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> pure parse/plan
#     core this route executes.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> domain
#     apply+audit execution this route delegates to.
#   - File: backend/ums_smart_revenue/connectors/google/
#     youtube_analytics_client.py -> list_target_channels reads cms_status and
#     content_owner_id to choose which channels a revenue pull targets.
# ============================================================================
@router.post("/import")
def import_channels(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    groups: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_import_audit_sink)],
    file: Annotated[UploadFile, File()],
    content_owner_id: Annotated[str, Form()],
    dry_run: Annotated[bool, Form()],
    reason: Annotated[str, Form()],
    cms_status: Annotated[str, Form()] = "INSIDE_CMS",
) -> ChannelImportResult:
    """Import a CMS channel roster CSV, previewing or applying every row."""
    target_scope = AccessScope.global_scope()
    if not has_permission(user, Permission.MANAGE_CHANNELS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_CHANNELS.value}",
        )
    content_owner_id = _validated_import_form(
        content_owner_id=content_owner_id, cms_status=cms_status, reason=reason
    )
    parsed = _parse_import_upload(file)

    # Group creation/membership is MANAGE_GROUPS territory. MANAGE_CHANNELS and
    # MANAGE_GROUPS are independently grantable, so a roster carrying Group_ID
    # values must also hold the group permission at the import's global scope —
    # otherwise the import would let a channels-only principal bypass the group
    # API's _require_manage_group_channels checks.
    if any(row.group_id for row in parsed.rows) and not has_permission(
        user, Permission.MANAGE_GROUPS, target_scope, org_index
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_GROUPS.value}",
        )

    wanted = {row.youtube_channel_id for row in parsed.rows}
    # include_inactive: an archived registry row must surface as a per-row
    # error in planning, not be mistaken for absent and planned as a CREATE
    # that create_channel's duplicate guard turns into a 500 mid-apply.
    existing = {
        entry.youtube_channel_id: entry
        for entry in registry.list_channels_by_ids(wanted, include_inactive=True)
    }
    plan = plan_channel_import(
        rows=parsed.rows,
        errors=parsed.errors,
        existing=existing,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
        # A row targeting an archived CMS group fails closed at planning:
        # attaching members to a retired group would audit a change that
        # active listings and finance scope selection never surface.
        archived_group_ids=_archived_group_ids(groups, parsed),
    )
    payload = _import_plan_to_api(
        plan, dry_run=dry_run, content_owner_id=content_owner_id, cms_status=cms_status
    )

    if plan.has_errors and not dry_run:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=payload)
    if dry_run:
        return ChannelImportResult.model_validate(payload)

    try:
        apply_channel_import(
            plan,
            registry=registry,
            groups=groups,
            audit_sink=audit_sink,
            actor=user,
            scope=target_scope,
            content_owner_id=content_owner_id,
            cms_status=cms_status,
            reason=reason,
            filename=file.filename,
        )
    # A revenue_required flip rejected by the locked-month guard aborts the
    # whole request; the import's single-transaction wiring rolls every prior
    # row back, so 409 (not a partial apply) is the honest outcome.
    except ChannelRevenueRequirementLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ChannelImportResult.model_validate(payload)


def _validated_import_form(*, content_owner_id: str, cms_status: str, reason: str) -> str:
    """Validate the import's scalar form fields; return the normalized owner.

    The owner is stripped once at this boundary so the plan, the registry
    writes, and the audit records all carry the exact value the SQL layer
    persists; a padded " owner-1 " must not diff against a stored "owner-1"
    as an UPDATE forever.
    """
    if cms_status not in IMPORTABLE_CMS_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid cms_status: {cms_status!r}",
        )
    content_owner_id = content_owner_id.strip()
    if not content_owner_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_owner_id is required",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason is required",
        )
    return content_owner_id


def _parse_import_upload(file: UploadFile) -> ParsedChannelImport:
    """Read, decode, parse, and size-cap the uploaded roster CSV.

    The row cap is enforced INSIDE the parser (max_rows), which aborts the
    moment the cap is exceeded instead of paying full per-row validation for
    an oversized file and only then counting.
    """
    raw = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"CSV exceeds {MAX_IMPORT_BYTES} bytes",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="CSV must be UTF-8 encoded",
        ) from exc
    try:
        return parse_channel_import_csv(text, max_rows=MAX_IMPORT_ROWS)
    except ChannelImportFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _archived_group_ids(
    groups: ChannelGroupRegistryStore, parsed: ParsedChannelImport
) -> frozenset[str]:
    """Return the roster's CMS group keys whose existing group is archived."""
    group_ids = {row.group_id for row in parsed.rows if row.group_id}
    return frozenset(
        group_id
        for group_id in group_ids
        if (group := groups.get_group_by_cms_id(group_id)) is not None and not group.active
    )


def _import_plan_to_api(
    plan: ChannelImportPlan, *, dry_run: bool, content_owner_id: str, cms_status: str
) -> dict[str, object]:
    """Render an import plan as the API response body.

    Every row echoes the planned inventory values (name, group, revenue flag),
    not just the field diff: a CREATE entry has an empty ``changes`` mapping by
    design, and the dry run's whole purpose is letting the operator verify the
    exact values a full-roster apply would write. The request-level owner and
    CMS status are echoed at the top level for the same reason.
    """
    return {
        "dry_run": dry_run,
        "content_owner_id": content_owner_id,
        "cms_status": cms_status,
        "counts": dict(plan.counts),
        "rows": [
            {
                "row_number": entry.row_number,
                "youtube_channel_id": entry.youtube_channel_id,
                "outcome": entry.outcome.value,
                "channel_name": entry.channel_name,
                "group_id": entry.group_id,
                "revenue_required": entry.revenue_required,
                "changes": {
                    name: {"from": pair[0], "to": pair[1]} for name, pair in entry.changes.items()
                },
                "reason": entry.reason,
            }
            for entry in plan.entries
        ],
    }


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
    can_manage_target = has_permission(user, Permission.MANAGE_ORG_MAPPING, target_scope, org_index)
    if not (can_manage_current and can_manage_target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_ORG_MAPPING.value}",
        )

    current_channel = registry.get_channel(youtube_channel_id)
    if current_channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

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
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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


# ============================================================================
# Purpose: Set or clear a channel's CMS content_owner_id. This is channel
#   ingestion configuration (which CMS account future revenue pulls target),
#   distinct from the org re-parenting handled by PATCH .../mapping.
# Database/ORM: YouTubeChannelORM via the registry's update_content_owner.
# Standards: Thin route — authorize -> existence check -> no-op detection ->
#   registry write -> typed-error translation -> CHANNEL_UPDATED audit. Authorize
#   before the existence check so an unauthorized caller never learns whether a
#   channel exists (no 404 information leak).
# Blast Radius: Future ingestion targeting only. No finance attribution rewrite,
#   no month locks, no Neo4j, no exports. CHANNEL_UPDATED is audited with an
#   explicit permission_override=MANAGE_CHANNELS so the audit record reflects
#   the permission that actually authorized this write (not the
#   registry.manage_org_mapping default on the CHANNEL_UPDATED definition).
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py
#     -> update_content_owner performs the tenant-scoped write.
#   - File: backend/ums_smart_revenue/connectors/google/
#     youtube_analytics_client.py -> list_target_channels reads content_owner_id
#     to choose which channels a revenue pull targets.
# ============================================================================
@router.patch("/{youtube_channel_id}/content-owner")
def update_channel_content_owner(
    youtube_channel_id: str,
    payload: ContentOwnerUpdateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    # Setting the CMS content owner is channel ingestion configuration, gated on
    # MANAGE_CHANNELS (not the org-re-parenting MANAGE_ORG_MAPPING). Authorize
    # before the existence check so a missing channel never leaks via a 404.
    target_scope = AccessScope.channel(youtube_channel_id)
    if not has_permission(user, Permission.MANAGE_CHANNELS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_CHANNELS.value}",
        )

    current_channel = registry.get_channel(youtube_channel_id)
    if current_channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    # No-op: re-submitting the current value writes nothing and is not audited,
    # so idempotent retries do not produce a misleading CHANNEL_UPDATED event.
    if current_channel.content_owner_id == payload.content_owner_id:
        response = current_channel.to_api()
        response["audit_event"] = None
        return response

    try:
        updated = registry.update_content_owner(
            youtube_channel_id=youtube_channel_id,
            content_owner_id=payload.content_owner_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        ) from exc
    # Translate a registry validation failure (e.g. an IntegrityError converted
    # during flush) to 422, mirroring create_channel/update_channel_mapping so a
    # bad payload surfaces as a client error instead of an unhandled 500.
    except ChannelRegistryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id=youtube_channel_id,
        scope=target_scope,
        reason=payload.reason,
        # FIX: This route authorizes on MANAGE_CHANNELS, but the CHANNEL_UPDATED
        # audit definition defaults to registry.manage_org_mapping (the mapping
        # re-parenting permission). Without an override the audit record
        # misattributes the authorizing permission, so an auditor filtering by
        # MANAGE_CHANNELS would miss every content-owner change. Both permissions
        # are SENSITIVE, so _resolve_audit_permission accepts this override.
        permission_override=Permission.MANAGE_CHANNELS,
        details={
            "old_content_owner_id": current_channel.content_owner_id,
            "new_content_owner_id": updated.content_owner_id,
        },
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
