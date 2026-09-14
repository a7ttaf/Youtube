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
"""Channel registry, bulk-import, and CMS group sync HTTP routes."""

import hashlib
import json
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
    resolve_tenant_uuid,
)
from ums_smart_revenue.api.dependencies_audit import (
    audit_record_to_api,
    current_atomic_audit_sink,
    current_audit_sink,
)
from ums_smart_revenue.api.dependencies_finance import current_org_access_index
from ums_smart_revenue.api.registry_dependencies import (
    current_channel_registry,
    sql_group_registry_from_session,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import (
    AuditSink,
    record_audit_event,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.youtube_groups_client import YouTubeGroupsClient
from ums_smart_revenue.connectors.runs.group_sync import (
    GroupSyncConflictRefusedError,
    GroupSyncFetchError,
    default_groups_client_factory,
    run_group_sync,
)
from ums_smart_revenue.connectors.runs.orchestrator import resolve_connector_credentials
from ums_smart_revenue.org.channel_group_sync import GroupSyncPlan
from ums_smart_revenue.org.channel_group_sync_apply import GroupSyncAppliedEntry
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupConflictError,
    ChannelGroupOwnerReassignmentError,
    ChannelGroupRegistryStore,
)
from ums_smart_revenue.org.channel_import import (
    ChannelImportFormatError,
    ChannelImportPlan,
    ParsedChannelImport,
    parse_channel_import_csv,
)
from ums_smart_revenue.org.channel_import_apply import (
    ChannelImportAdoptableGroupError,
    ChannelImportArchivedGroupError,
    ChannelImportGroupActionDivergedError,
    ChannelImportGroupOwnerMismatchError,
    ChannelImportRowStateDivergedError,
    apply_channel_import,
    plan_channel_import_with_stores,
)
from ums_smart_revenue.org.channel_issues import (
    build_channel_registry_issues,
    summarize_channel_registry_issues,
)
from ums_smart_revenue.org.channel_registry import (
    ChannelMappingLockedMonthError,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryStore,
    ChannelRegistryValidationError,
    ChannelRevenueRequirementLockedMonthError,
)

router = APIRouter(prefix="/channels", tags=["channels"])

_OFFICIAL_REVENUE_SOURCE_STATUSES = frozenset({"OFFICIAL_CMS_REVENUE", "OFFICIAL_MANUAL_IMPORT"})

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
# Generous bound for CMS content-owner ids (~22 chars in practice); the value
# becomes audit_logs.entity_id inside the ix_audit_logs_entity B-tree index.
MAX_CONTENT_OWNER_CHARS = 255
# Mirrors the youtube_channels cms_status CHECK in 20260510_0002_org_registry.
IMPORTABLE_CMS_STATUSES = frozenset({"INSIDE_CMS", "OUTSIDE_CMS", "UNKNOWN"})


class ChannelCreateRequest(BaseModel):
    """Request body for POST /channels — create one channel registry entry."""

    youtube_channel_id: str = Field(min_length=1)
    channel_name: str = Field(min_length=1)
    primary_company_id: str = Field(min_length=1)
    cms_status: str
    revenue_required: bool
    content_owner_id: str | None = None

    @field_validator("youtube_channel_id", "channel_name", "primary_company_id", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        """Strip the required string fields via the shared strip/blank-reject rule."""
        return _strip_required_string(value)

    @field_validator("content_owner_id", mode="before")
    @classmethod
    def strip_optional_content_owner(cls, value: object) -> object:
        """Strip the optional content owner field via the shared strip/blank-reject rule."""
        return _strip_optional_string(value)


class ChannelMappingRequest(BaseModel):
    """Request body for PATCH .../mapping — re-parent a channel to a new company."""

    primary_company_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("primary_company_id", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        """Strip the required string fields via the shared strip/blank-reject rule."""
        return _strip_required_string(value)


class ContentOwnerUpdateRequest(BaseModel):
    """Request body for PATCH .../content-owner — set or clear a channel's CMS owner."""

    # content_owner_id is required-to-be-present but nullable: sending null
    # clears the CMS content owner; a present-but-blank string is rejected.
    content_owner_id: str | None
    reason: str = Field(min_length=1)

    @field_validator("content_owner_id", mode="before")
    @classmethod
    def strip_optional_content_owner(cls, value: object) -> object:
        """Strip the optional content owner field via the shared strip/blank-reject rule."""
        return _strip_optional_string(value)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value: object) -> object:
        """Strip the required reason field via the shared strip/blank-reject rule."""
        return _strip_required_string(value)


class ChannelImportFieldChange(BaseModel):
    """One field's before/after pair in an import row's diff."""

    model_config = ConfigDict(populate_by_name=True)

    from_value: str | bool | None = Field(alias="from")
    to_value: str | bool | None = Field(alias="to")


class ChannelImportSourceStatusChange(BaseModel):
    """The revenue_source_status transition a row's write will perform.

    Separate from ChannelImportFieldChange because ``from`` is genuinely
    absent for a CREATE — the channel has no prior classification — where the
    inventory diff's pairs always have both sides.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_value: str | None = Field(alias="from")
    to_value: str = Field(alias="to")


class ChannelImportRowResult(BaseModel):
    """One CSV row's planned or applied outcome.

    ``outcome``, ``changes``, ``group_id`` and ``group_action`` describe every
    write the row performs — there is no ownership write hiding behind them.
    An import never claims an existing group for its content owner (a row
    targeting an unowned group is an ERROR naming the sync remedy), so the
    only stamp it can write belongs to a group the same row creates.
    """

    row_number: int
    youtube_channel_id: str | None
    outcome: str
    channel_name: str | None
    group_id: str | None
    # "CREATE" (this row mints a new SECTOR group, stamped to the request's
    # content owner at birth) or "JOIN" (the owner already holds the group;
    # the row attaches the channel to it unless it is already a member).
    # Null when the row carries no group_id, and on every ERROR row — those
    # write nothing at all. The literal set is ChannelImportGroupAction
    # (channel_import.py); the field itself is a plain str, matching how
    # `outcome` renders its own enum.
    group_action: str | None
    revenue_required: bool | None
    changes: dict[str, ChannelImportFieldChange]
    # The revenue_source_status this row's write will leave on the channel,
    # when it changes it. Kept OUT of `changes` deliberately: that map holds
    # the operator's own field edits and is what the write-boundary pre-state
    # guard compares against, whereas this value is DERIVED by the registry
    # from the revenue_required flip. Disclosed because it drives
    # `missing_official_revenue` and the registry's recommended action, so a
    # preview omitting it asks the operator to approve a finance-source
    # mutation the diff never mentions (review #184). `from` is null for a
    # CREATE; the whole field is null when the write leaves the status alone.
    revenue_source_status: ChannelImportSourceStatusChange | None
    reason: str | None


class ChannelImportResult(BaseModel):
    """Declared response contract for POST /channels/import (dry run and apply)."""

    dry_run: bool
    content_owner_id: str
    cms_status: str
    counts: dict[str, int]
    rows: list[ChannelImportRowResult]
    # Digest of the plan content above (`counts` + `rows`) AND the TARGET the
    # write lands in: `content_owner_id`, `cms_status`, and the server-resolved
    # tenant. A client echoes a dry run's value back as
    # `expected_plan_fingerprint` on the apply, and a mismatch is a 409: the
    # apply re-plans from CURRENT state, so without this a row reviewed as
    # CREATE could silently commit as an UPDATE over a concurrently created
    # channel (review #184).
    #
    # The target inputs are LOAD-BEARING, not incidental. An all-CREATE roster's
    # rows carry no owner (a CREATE's `changes` is empty by design), so a digest
    # over content alone let a preview approved for owner A authorize the same
    # plan against owner B; the tenant is in for the same reason across
    # tenancies. It is server-resolved and never client-supplied, which is what
    # makes it a boundary rather than an echo — and it is why this digest is
    # NOT reproducible client-side. It is an opaque equality token to echo
    # back, not a checksum to recompute. Only `dry_run` is outside it, because
    # a preview and its apply differ in it by definition.
    plan_fingerprint: str
    # The COMPANION digest, covering exactly the DISCLOSED reviewed set —
    # `counts`, `rows`, `content_owner_id`, `cms_status` — and nothing the
    # response does not show. That scope is the point: a client can recompute
    # it from the fields it renders (canonical JSON, sorted keys, tight
    # separators, SHA-256) and so verify that the token it binds with describes
    # the plan it actually displayed, which the opaque fingerprint above cannot
    # offer by design. Echoed back as `expected_display_digest` on the apply; a
    # mismatch against the CURRENT plan's digest is a 409 carrying the
    # refreshed plan. NOT a tenancy boundary — the tenant is deliberately
    # outside it (that exclusion is what makes it recomputable), and
    # cross-tenant binding stays `plan_fingerprint`'s job (review #184, C1).
    display_digest: str


class GroupSyncGroupResult(BaseModel):
    """One CMS group's planned or applied result.

    Identical in shape for both modes; only the SOURCE differs. A dry run
    renders these from the plan ("what would happen"), an apply from the write
    boundary ("what did") — which is why every field is nullable/empty-able
    rather than carrying mode-specific variants.
    """

    cms_group_id: str
    outcome: str
    title: str | None
    local_group_id: str | None
    # Exactly [from, to]. Typed as a 2-tuple rather than a bare list so the
    # arity is part of the contract and not just a convention: Pydantic
    # validates the length here and still serializes a JSON array, so the wire
    # format is unchanged while a 1- or 3-element pair becomes a boundary
    # error instead of something a client has to guess about.
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: list[str]
    members_removed: list[str]
    # Capped at 50 ids; the count is the untruncated total.
    unknown_channel_ids: list[str]
    unknown_channel_count: int
    # "will adopt" on a preview, "did adopt" on an apply — the same
    # mode-dependence `counts` carries.
    will_adopt_content_owner: bool


class GroupSyncResult(BaseModel):
    """Declared response contract for POST /channels/groups/sync.

    Exists so the API boundary is validated rather than hand-assembled: this
    surface shipped two defects in review where the response disagreed with
    the audit trail (plan counts vs executed counts, then plan per-group diffs
    vs executed ones), and an unstructured dict cannot catch that class of
    drift at the boundary or in the generated OpenAPI contract.
    """

    dry_run: bool
    content_owner_id: str
    counts: dict[str, int]
    unknown_channel_total: int
    non_channel_member_count: int
    groups: list[GroupSyncGroupResult]


def _strip_required_string(value: object) -> object:
    """Strip a required string value; reject it if blank after stripping."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


def _strip_optional_string(value: object) -> object:
    """Strip an optional string value; None passes through, blank-after-strip is rejected."""
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


@router.get("")
def list_channels(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> list[dict[str, object]]:
    """List every channel the caller is authorized to view for analytics."""
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
    """List outside-CMS channels the caller can view, with a revenue-required summary."""
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
    """List registry health issues across the caller's visible channels and groups."""
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
    """Raise 403 unless the user holds VIEW_ANALYTICS in some scope."""
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
    """Return the channels the user may view for analytics, filtered by scope."""
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
    """Render one outside-CMS channel plus its missing-revenue flag and recommendation."""
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
    """Recommend the next operator action for an outside-CMS channel's revenue state."""
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
    """Return the channel ids the user's VIEW_ANALYTICS scopes authorize, or None for global."""
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
    """Return the user's active direct-grant scopes for one permission."""
    return [
        grant.scope
        for grant in user.direct_permissions
        if grant.active and grant.permission == permission
    ]


def _role_scopes_for_permission(user: UserPrincipal, permission: Permission) -> list[AccessScope]:
    """Return the scopes the user's active roles grant for one permission."""
    return [
        assignment.scope
        for assignment in user.role_assignments
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset())
    ]


def _granted_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> tuple[AccessScope, ...]:
    """Return every scope — direct and role-granted — that grants the user one permission."""
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
    """Create one channel registry entry and record a CHANNEL_CREATED audit event."""
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
#   ChannelGroupRegistryStore. Finance tables are READ, never written: the
#   registry's revenue_required guard reads FinanceMonthCloseORM x
#   MonthlyChannelRevenueFactORM to reject an OFF->ON flip that a LOCKED month
#   has no fact for.
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
#   would silently drop a newly added Group_ID on re-import. The RESPONSE is
#   the plan echo and is deliberately identical in shape and content for a dry
#   run and an apply — that equivalence is what makes the dry run a truthful
#   preview. The durable record of what a concurrent writer actually left this
#   apply to do is the AUDIT trail, whose CHANNEL_IMPORTED counts are
#   accumulated at the write boundary, not copied from the plan.
# Blast Radius: Connector ingest targeting. list_target_channels only pulls
#   revenue for cms_status='INSIDE_CMS' channels, so importing a roster with
#   the wrong cms_status silently removes channels from ingest and their
#   revenue simply stops arriving with no error — the dry-run diff is the
#   operator's guard against that. ALSO month-close: an apply runs inside the
#   month-close protocol. Every registry write takes the tenant-wide
#   REVENUE_REQUIREMENT_GUARD_MONTH advisory lock before any row lock, so an
#   import and a concurrent month close serialize against each other (an
#   import can block on a close, and vice versa), and a revenue_required
#   OFF->ON flip is rejected with 409 when a LOCKED month has no fact for the
#   channel. Roster imports are therefore INSIDE month-close validation, not
#   outside it. No allocation, no exports, no finance WRITES.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> pure parse/plan
#     core this route executes.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> domain
#     apply+audit execution this route delegates to.
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> holds the
#     close guard and runs the LOCKED-month revenue_required flip check.
#   - File: backend/ums_smart_revenue/finance/month_close_locks.py -> the
#     advisory guard and shared database clock the apply serializes on.
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
    audit_sink: Annotated[AuditSink, Depends(current_atomic_audit_sink)],
    file: Annotated[UploadFile, File()],
    content_owner_id: Annotated[str, Form()],
    dry_run: Annotated[bool, Form()],
    reason: Annotated[str, Form()],
    cms_status: Annotated[str, Form()] = "INSIDE_CMS",
    expected_plan_fingerprint: Annotated[str | None, Form()] = None,
    expected_display_digest: Annotated[str | None, Form()] = None,
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

    plan = plan_channel_import_with_stores(
        parsed,
        registry=registry,
        groups=groups,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
    )
    payload = _import_plan_to_api(
        plan,
        dry_run=dry_run,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
        # The RESOLVED tenant, not a client-supplied echo: an approval obtained
        # in one tenant must not be spendable in another.
        tenant_id=str(resolve_tenant_uuid(user)),
    )

    if dry_run:
        return ChannelImportResult.model_validate(payload)

    # The apply re-plans from CURRENT state, so the plan about to execute is
    # not necessarily the one the operator reviewed: a concurrent writer can
    # turn a reviewed CREATE into an UPDATE that overwrites their new channel,
    # or a group CREATE into a JOIN. Binding the apply to the reviewed plan's
    # fingerprint makes that divergence a retryable 409 carrying the REFRESHED
    # plan, so approval is re-sought against reality instead of a stale
    # preview (review #184). Optional for API clients that never previewed —
    # they are not re-approving anything — but the SPA always sends it.
    # FIX: Binding-token mismatches run BEFORE the ERROR-row 422 branch so a
    # stale digest/fingerprint is classified as plan drift (409 + refreshed
    # plan), not as a roster-validation failure (422).
    if (
        expected_plan_fingerprint is not None
        and expected_plan_fingerprint != payload["plan_fingerprint"]
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=payload)
    # The same guard over the DISCLOSED digest. A client that recomputed the
    # digest from the plan it rendered — rather than trusting the token the
    # response carried — binds here, so a response that showed one plan while
    # carrying another plan's tokens no longer has a token the client will
    # echo. The two checks are deliberately independent: either token alone
    # binds the apply, and the SPA sends both (review #184, C1).
    if expected_display_digest is not None and expected_display_digest != payload["display_digest"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=payload)

    if plan.has_errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=payload)

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
            # A client that bound its apply to a reviewed plan is saying
            # "apply the diff I reviewed, or none of it", so a row whose
            # pre-state moved in the re-plan-to-row-lock window fails closed
            # rather than overwriting a value the operator never saw. Off for
            # unbound callers, who are re-approving nothing and keep the
            # documented default (the file wins). EITHER token opts in: a
            # digest-bound caller reviewed a plan exactly as a
            # fingerprint-bound one did.
            enforce_reviewed_pre_state=(
                expected_plan_fingerprint is not None or expected_display_digest is not None
            ),
        )
    # A group whose owner stamp was cleared between the plan and the write is
    # the one plan-to-apply race the operator's preview cannot have shown: the
    # dry run reported a clean row because the group WAS owned. Canned detail
    # rather than str(exc) — the remedy is fixed (run the owner's sync, then
    # retry), so nothing in the exception text needs to reach the client.
    except ChannelImportAdoptableGroupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A CMS group in this roster lost its content owner during the "
                "import; run POST /channels/groups/sync for this content owner "
                "to adopt it, then retry the import"
            ),
        ) from exc
    # Expected apply-time domain failures abort the whole request as 409, not
    # 500: a revenue_required flip rejected by the locked-month guard, a group
    # archived in the plan-to-apply window, or a uniqueness race lost to a
    # concurrent writer (channel planned as CREATE that now exists; two
    # imports both creating the same CMS group). The import's
    # single-transaction wiring rolls every prior row back, so 409 (a
    # client-retryable conflict, not a partial apply) is the honest outcome.
    except (
        ChannelGroupConflictError,
        ChannelImportArchivedGroupError,
        ChannelImportGroupActionDivergedError,
        ChannelImportGroupOwnerMismatchError,
        ChannelImportRowStateDivergedError,
        ChannelRegistryConflictError,
        ChannelRevenueRequirementLockedMonthError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ChannelImportResult.model_validate(payload)


def _validated_content_owner_id(raw: str) -> str:
    """Validate one content_owner_id value; return the normalized (stripped) id.

    Shared by the CSV import route and the CMS group sync route so both
    boundaries enforce the identical non-blank / NUL / length contract before
    the value reaches a registry write, a Google credential lookup, or
    becomes audit_logs.entity_id.
    """
    content_owner_id = raw.strip()
    if not content_owner_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_owner_id is required",
        )
    # PostgreSQL cannot store NUL in a text column (youtube_channels.
    # content_owner_id); rejecting it here keeps the promised 422 contract
    # instead of an unhandled encoding 500 at the write boundary — the same
    # rule the CSV parser applies to per-row text fields.
    if "\x00" in content_owner_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_owner_id contains a NUL character",
        )
    # The owner id becomes audit_logs.entity_id, which sits in the
    # ix_audit_logs_entity B-tree; PostgreSQL rejects index entries past its
    # per-entry size limit, so an unbounded value would pass earlier checks
    # and 500 at the final audit append. Real CMS owner ids are ~22 characters.
    if len(content_owner_id) > MAX_CONTENT_OWNER_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"content_owner_id exceeds {MAX_CONTENT_OWNER_CHARS} characters",
        )
    return content_owner_id


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
    content_owner_id = _validated_content_owner_id(content_owner_id)
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason is required",
        )
    # PostgreSQL cannot store NUL in a text column (audit_logs.reason);
    # rejecting it here keeps the promised 422 contract instead of an
    # unhandled encoding 500 at apply — the same rule the CSV parser applies
    # to per-row text fields.
    if "\x00" in reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason contains a NUL character",
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


# ============================================================================
# Purpose: Render an import plan as the API response body — which is the same
#   object twice over: the disclosure the operator approves, and the material
#   ``_plan_fingerprint`` binds an apply to. The relationship runs ONE way and
#   the asymmetry is deliberate. Everything the operator REVIEWS is inside the
#   token, so nothing they approved can change under a bound apply; but the
#   token is not limited to what is shown. It also covers the server-resolved
#   ``tenant_id``, which is withheld from this body on purpose — a client that
#   could see it is a client that could try to name it. So adding a field HERE
#   means adding it to the digest, while adding one to the DIGEST obliges no
#   disclosure, and for the tenant must not. Do not "restore symmetry" by
#   exposing the tenant or by dropping it from the digest: the first hands the
#   client a value it must not choose, the second lets a plan reviewed in one
#   tenant authorize a write in another.
# Database/ORM: None — a pure projection of an already-computed plan. It issues
#   no query and re-reads nothing; ``tenant_id`` arrives resolved.
# Standards: Every row echoes the planned inventory VALUES, not just the field
#   diff — a CREATE's ``changes`` is empty by design, and the dry run exists so
#   the operator can see the exact values a full-roster apply would write.
#   ``revenue_source_status`` is disclosed OUTSIDE ``changes`` on purpose: it is
#   derived by the write rather than asserted by the CSV, and ``changes`` is
#   what the write-boundary pre-state guard compares, so folding it in would
#   make that guard police a field the roster never claimed. ``rows`` is
#   annotated rather than inferred because ``list`` is invariant and the
#   inferred element type would not satisfy ``_plan_fingerprint``. Both modes
#   render the PLAN — unlike the sync route, whose apply renders the write
#   boundary's own record.
# Blast Radius: FINANCE + TENANCY + write authorization. These counts are the
#   APPROVED PLAN, never the committed result: the apply re-checks every row
#   under its write-boundary lock and tallies what it actually wrote into the
#   ``CHANNEL_IMPORTED`` event, so a concurrent writer can turn a planned
#   UPDATE into a no-op. Consumers must label them as the plan — the SPA's
#   Applied step does. The tenant reaching the digest here is what stops a plan
#   reviewed in one tenant from authorizing a write in another.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> _plan_fingerprint,
#     which digests exactly this payload plus the resolved tenant.
#   - File: backend/ums_smart_revenue/org/channel_import.py ->
#     ChannelImportPlan / ChannelImportPlanEntry, the source of every field.
#   - File: frontend/src/lib/api/useChannelImport.ts -> isChannelImportResult,
#     the client-side structural gate that mirrors this shape field for field.
#   - File: Docs/12_BACKEND_API_SPEC.md -> the documented response contract.
# ============================================================================
def _import_plan_to_api(
    plan: ChannelImportPlan,
    *,
    dry_run: bool,
    content_owner_id: str,
    cms_status: str,
    tenant_id: str,
) -> dict[str, object]:
    """Render an import plan as the API response body.

    Every row echoes the planned inventory values (name, group, revenue flag),
    not just the field diff: a CREATE entry has an empty ``changes`` mapping by
    design, and the dry run's whole purpose is letting the operator verify the
    exact values a full-roster apply would write. The request-level owner and
    CMS status are echoed at the top level for the same reason.

    Both modes render the PLAN — unlike the sync route, whose apply renders the
    write boundary's own record. No KIND of write hides behind it: the import
    claims no existing group's ownership, and ``group_action`` now names the
    one remaining effect ``group_id`` alone left ambiguous (mint a new SECTOR
    group vs attach to this owner's existing one). The plan is still not a
    re-read, though: the apply re-checks every row under its write-boundary
    lock and tallies what it ACTUALLY wrote into the CHANNEL_IMPORTED audit
    event, so a concurrent writer can make a planned UPDATE a no-op. Consumers
    must present these counts as the approved plan, not as the committed
    result — the SPA's Applied step labels them exactly that way.
    """
    counts = dict(plan.counts)
    # Annotated rather than inferred: list is INVARIANT, so the literal's
    # inferred list[dict[str, <union of cell types>]] is not a
    # list[dict[str, object]] and would not satisfy _plan_fingerprint.
    rows: list[dict[str, object]] = [
        {
            "row_number": entry.row_number,
            "youtube_channel_id": entry.youtube_channel_id,
            "outcome": entry.outcome.value,
            "channel_name": entry.channel_name,
            "group_id": entry.group_id,
            "group_action": (entry.group_action.value if entry.group_action is not None else None),
            "revenue_required": entry.revenue_required,
            "changes": {
                name: {"from": pair[0], "to": pair[1]} for name, pair in entry.changes.items()
            },
            # Disclosed separately from `changes` on purpose: the source status
            # is DERIVED by the write, not carried by the CSV, and `changes`
            # holds the operator's own field edits (it is also what the
            # write-boundary pre-state guard compares against). Folding a
            # derived value in there would make the guard police a field the
            # roster never asserted. Null when the write leaves it alone.
            "revenue_source_status": (
                {
                    "from": entry.revenue_source_status[0],
                    "to": entry.revenue_source_status[1],
                }
                if entry.revenue_source_status is not None
                else None
            ),
            "reason": entry.reason,
        }
        for entry in plan.entries
    ]
    return {
        "dry_run": dry_run,
        "content_owner_id": content_owner_id,
        "cms_status": cms_status,
        "counts": counts,
        "rows": rows,
        "plan_fingerprint": _plan_fingerprint(
            counts,
            rows,
            content_owner_id=content_owner_id,
            cms_status=cms_status,
            tenant_id=tenant_id,
        ),
        "display_digest": _display_digest(
            counts,
            rows,
            content_owner_id=content_owner_id,
            cms_status=cms_status,
        ),
    }


# ============================================================================
# Purpose: Digest one import plan into the equality token an apply binds to,
#   so a client can say "execute the plan I reviewed, or nothing".
# Database/ORM: None — pure function over an already-rendered plan payload.
# Standards: The digest is the CONTRACT, so its inputs are a change point:
#   anything an operator reviews must be inside it (plan rows, counts, the
#   content owner + CMS status the write targets, and the RESOLVED tenant it
#   lands in) and anything that legitimately differs between a preview and its
#   apply must be outside it (`dry_run`).
#   Widening or narrowing this set silently changes what "same plan" means —
#   omitting the target once let an apply commit under a content owner that was
#   never reviewed (review #184). Canonical JSON (sort_keys + tight separators)
#   keeps the token stable across dict ordering and Python versions. It is an
#   equality token only: never a secret, never an authorization input, so a
#   plain SHA-256 is the whole mechanism and no constant-time compare applies.
# Blast Radius: Which applies are accepted vs rejected 409, and — because
#   sending the token opts in to write-boundary pre-state enforcement — whether
#   a diverged row rolls the import back. No writes of its own.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> import_channels
#     compares it against expected_plan_fingerprint before the apply.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     enforce_reviewed_pre_state, which that comparison switches on.
#   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> sends
#     the reviewed plan's token and re-binds to the refreshed plan on 409.
# ============================================================================
def _plan_fingerprint(
    counts: dict[str, int],
    rows: list[dict[str, object]],
    *,
    content_owner_id: str,
    cms_status: str,
    tenant_id: str,
) -> str:
    """Digest everything an operator reviews: the plan AND the target it targets.

    ``content_owner_id`` and ``cms_status`` are IN the digest, not treated as
    mere echoes of form fields the apply re-sends. They are reviewed values —
    the preview step names the content owner on screen — and leaving them out
    let an apply bind to a different target than the one reviewed: the SPA's
    owner picker stayed live during the dry run, so an operator could preview
    owner A, switch to B while the request was in flight, and apply against B.
    On an all-CREATE roster the rows carry no owner (a CREATE's ``changes`` is
    empty by design), so B's plan digested identically to A's and the guard
    waved it through, committing channels and groups under the wrong content
    owner (review #184).

    ``tenant_id`` is in for the same reason and one step further out: it is the
    resolved tenant, never a client echo. Without it, two EMPTY tenants and an
    all-CREATE roster digest identically — a CREATE's ``changes`` is empty by
    design and the rows carry no tenant — so a preview approved in tenant A
    satisfied the guard on an apply directed at tenant B, and channels, groups
    and audit records committed there on an approval that was never given for
    them (review #184). Tenancy is the one boundary an equality token must
    never straddle.

    ``dry_run`` stays out, and that exclusion is load-bearing: a preview and
    its apply differ in it by definition, so folding it in would make every
    fingerprint mismatch — and a guard that always fires protects nothing.

    ``sort_keys`` plus tight separators make this stable across dict ordering
    and Python versions, so the same plan always digests the same way. It is an
    equality token, never a secret and never an authorization input, so a plain
    SHA-256 of the canonical JSON is the whole mechanism.
    """
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "content_owner_id": content_owner_id,
            "cms_status": cms_status,
            "counts": counts,
            "rows": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Purpose: Digest the DISCLOSED half of one import plan — exactly the fields
#   the response shows — so a client can verify that the plan it rendered is
#   the plan its binding token describes, by recomputing rather than trusting.
# Database/ORM: None — pure function over an already-rendered plan payload.
# Standards: The input set is exactly `plan_fingerprint`'s MINUS the resolved
#   tenant, and that subtraction is the whole design: the tenant is the one
#   digest input the response does not disclose, so it is the one input that
#   made the fingerprint unrecomputable. Everything here is on screen —
#   counts, rows, content owner, CMS status — which makes this digest
#   recomputable BY CONSTRUCTION (canonical JSON: sorted keys, tight
#   separators, ensure_ascii default; SHA-256 of the UTF-8 bytes). The
#   recomputability is pinned by a test that rebuilds it from a response body
#   with nothing but hashlib+json. `dry_run` stays out for the fingerprint's
#   reason: a preview and its apply differ in it by definition. An equality
#   token, never a secret and never an authorization input — no constant-time
#   compare applies.
# Blast Radius: Which applies are accepted vs rejected 409 for callers binding
#   via `expected_display_digest`, and (with the fingerprint) whether the
#   write-boundary pre-state guard is on. NOT a tenancy boundary: two tenants
#   previewing identical rosters get EQUAL display digests on purpose —
#   cross-tenant binding is `plan_fingerprint`'s job, and both checks run.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> import_channels
#     compares it against expected_display_digest before the apply, and
#     _plan_fingerprint above is the opaque companion whose input set this
#     mirrors minus the tenant.
#   - File: frontend/src/lib/api/useChannelImport.ts -> requires the field on
#     every accepted plan and echoes it on the bound apply.
#   - File: Docs/12_BACKEND_API_SPEC.md -> documents the recomputation recipe
#     as part of the import contract.
# ============================================================================
def _display_digest(
    counts: dict[str, int],
    rows: list[dict[str, object]],
    *,
    content_owner_id: str,
    cms_status: str,
) -> str:
    """Digest exactly what the response DISCLOSES, so a client can recompute it.

    ``plan_fingerprint`` is deliberately opaque: the resolved tenant inside it
    is a boundary precisely because clients cannot supply or reproduce it. The
    cost of that opacity is that a client cannot check the token against the
    plan it is looking at — a response that rendered one plan while carrying
    another plan's fingerprint would be echoed back without complaint. This
    digest closes that gap from the other side: every input is a field the
    response body shows, so a client that recomputes it from what it rendered
    — instead of echoing the token it received — binds the apply to the plan
    it actually displayed (review #184, C1).

    The tenant's exclusion is therefore load-bearing, and it is NOT a leak of
    tenancy protection: ``expected_display_digest`` and
    ``expected_plan_fingerprint`` are independent checks, so a cross-tenant
    replay still fails the fingerprint compare whenever the SPA (which always
    sends both) is the caller — and a digest-only caller is bound to the
    disclosed plan, which is all that token ever claimed to cover.

    Same canonical form as the fingerprint — ``sort_keys`` plus tight
    separators over the UTF-8 bytes, plain SHA-256 — so the recomputation
    recipe is one sentence: sorted-key JSON of the four disclosed fields.
    """
    canonical = json.dumps(
        {
            "content_owner_id": content_owner_id,
            "cms_status": cms_status,
            "counts": counts,
            "rows": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GroupSyncRequest(BaseModel):
    """Operator request to mirror a content owner's CMS groups."""

    content_owner_id: str
    dry_run: bool
    reason: str


def current_groups_client_factory() -> Callable[[Credentials], YouTubeGroupsClient]:
    """Build a live groups client from resolved credentials (test-overridable).

    Delegates to the ``connectors.runs`` default factory so the scheduled worker
    (Sched 2) can build the same production client without importing ``api``.
    """
    return default_groups_client_factory


# ============================================================================
# Purpose: Mirror a YouTube CMS content owner's groups into channel_groups —
#   titles, membership (adds AND removals), deactivation of vanished groups,
#   reactivation of reappearing keys. Mandatory dry-run; YouTube wins.
# Database/ORM: ChannelGroupORM/ChannelGroupMemberORM via the group store;
#   ApiConnectorCredentialORM read via resolve_connector_credentials.
# Standards: Global MANAGE_GROUPS fail-closed (group writes must not bypass
#   the group API's authorization); fetch completes before any write; audit
#   runs on a PlatformLaneAuditSink bound to the request's TENANT session
#   (current_atomic_audit_sink), so GROUP_UPDATED/GROUPS_SYNCED rows share one
#   transaction with the group writes and a mid-apply failure OR a lost commit
#   rolls both back together; GROUPS_SYNCED summary uses ACTUAL apply counts,
#   never the plan's; canned error details only — Google/credential exception
#   text never reaches HTTP.
# Blast Radius: Group naming/membership/active state, audit. Finance
#   group-scope rollups change composition only as the CMS does. No channel
#   rows are ever created here (unknown members surface in the response).
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> planner.
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py -> apply.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_groups_client.py.
# ============================================================================
@router.post("/groups/sync", response_model=GroupSyncResult)
def sync_channel_groups(
    payload: GroupSyncRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    groups: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_atomic_audit_sink)],
    client_factory: Annotated[
        Callable[[Credentials], YouTubeGroupsClient], Depends(current_groups_client_factory)
    ],
) -> GroupSyncResult:
    """Mirror the content owner's CMS groups locally, previewing or applying."""
    target_scope = AccessScope.global_scope()
    if not has_permission(user, Permission.MANAGE_GROUPS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_GROUPS.value}",
        )
    content_owner_id = _validated_content_owner_id(payload.content_owner_id)
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason is required",
        )
    # PostgreSQL cannot store NUL in a text column (audit_logs.reason);
    # rejecting it here keeps the promised 422 contract instead of an
    # unhandled encoding 500 at the apply/audit boundary — the same rule
    # _validated_import_form applies to the bulk import's reason field.
    if "\x00" in reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason contains a NUL character",
        )

    try:
        result = run_group_sync(
            session,
            tenant_id=resolve_tenant_uuid(user),
            content_owner_id=content_owner_id,
            registry=registry,
            groups=groups,
            audit_sink=audit_sink,
            actor=user,
            reason=reason,
            dry_run=payload.dry_run,
            client_factory=client_factory,
            resolver=resolve_connector_credentials,
        )
    except (CredentialNotFoundError, InactiveCredentialError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No active youtube-analytics credential for this content owner; "
                "register one before syncing groups."
            ),
        ) from exc
    except OAuthRefreshError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google credential token refresh failed; "
                "check that the credential secret is current."
            ),
        ) from exc
    except GroupSyncFetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "YouTube groups fetch failed; check connector configuration and account access."
            ),
        ) from exc
    # FIX: A different GoogleConnectorError subclass (e.g. SecretFetchError) raised
    # inside resolve_connector_credentials previously escaped as a raw 500; mirror
    # the connector-test route's broad catch so any credential-layer failure fails
    # closed as 503 with a canned detail (str(exc) can embed secret locators). The
    # fetch phase raises GroupSyncFetchError (caught above), so this broad clause
    # now means the credential layer alone — preserving the 503-vs-502 split.
    except GoogleConnectorError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Credential resolution failed; check connector configuration and secret references."
            ),
        ) from exc
    # The foreign-owner refusal the mandatory dry run already previewed as
    # CONFLICT: run_group_sync rolled back and raised a typed error carrying the
    # exact detail, and the route maps it to the same 409 it used to raise inline.
    except GroupSyncConflictRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
    # A concurrent writer (another sync, or the bulk import) can win a uniqueness
    # race on the same CMS key between planning and apply; the store translates
    # that into the typed conflict instead of letting an IntegrityError-aborted
    # session escape as a 500. Mirrors import_channels' identical translation
    # earlier in this file.
    #
    # The reassignment error is the OTHER side of that same race: rather than
    # colliding on a new key, the racer CLAIMED a group this plan matched while
    # it was still owner-NULL. Only the apply layer's locked re-read can see it,
    # and it refuses to mirror another owner's group. Both are lost races on the
    # same row, both retryable, so both get the one 409 — matching the import
    # route's identical cross-owner rejection.
    except (ChannelGroupConflictError, ChannelGroupOwnerReassignmentError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    payload_out = _group_sync_plan_to_api(
        result.plan, dry_run=payload.dry_run, content_owner_id=content_owner_id
    )
    if result.execution is None:
        return GroupSyncResult.model_validate(payload_out)

    # An apply's response must agree with its audit rows, per group and in
    # aggregate. `payload_out` was rendered from the PLAN, which is right for a
    # dry run ("what will happen") but stale for an apply: a concurrent writer
    # can land the rename before the locked re-read, leaving the plan claiming
    # RENAME with a full diff while GROUPS_SYNCED recorded UNCHANGED and no
    # GROUP_UPDATED was written. Both surfaces now come from the write boundary.
    payload_out["counts"] = dict(result.execution.counts)
    payload_out["groups"] = _applied_entries_to_api(result.execution.entries)
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.GROUPS_SYNCED,
        entity_type="channel_group_sync",
        entity_id=content_owner_id,
        scope=target_scope,
        reason=reason,
        details={
            "content_owner_id": content_owner_id,
            "counts": dict(result.execution.counts),
            "unknown_channel_total": result.plan.unknown_channel_total,
            "non_channel_member_count": result.plan.non_channel_member_count,
        },
    )
    return GroupSyncResult.model_validate(payload_out)


def _applied_entries_to_api(entries: tuple[GroupSyncAppliedEntry, ...]) -> list[dict[str, object]]:
    """Render every applied result in the plan's group-object shape."""
    return [_applied_entry_to_api(entry) for entry in entries]


def _applied_entry_to_api(entry: GroupSyncAppliedEntry) -> dict[str, object]:
    """Render one group's ACTUAL result in the same shape the plan renders.

    Key-for-key identical to ``_group_sync_plan_to_api``'s group objects so the
    dry-run and apply payloads stay one shape; only the SOURCE differs, which
    is the whole point — a dry run reports what would happen, an apply reports
    what did. ``will_adopt_content_owner`` carries the same mode-dependence as
    ``counts``: "will" on a preview, "did" on an apply.
    """
    return {
        "cms_group_id": entry.cms_group_id,
        "outcome": entry.outcome.value,
        "title": entry.title,
        "local_group_id": entry.local_group_id,
        "name_change": list(entry.name_change) if entry.name_change else None,
        "active_change": list(entry.active_change) if entry.active_change else None,
        "members_added": list(entry.members_added),
        "members_removed": list(entry.members_removed),
        "unknown_channel_ids": list(entry.unknown_channel_ids[:50]),
        "unknown_channel_count": len(entry.unknown_channel_ids),
        "will_adopt_content_owner": entry.adopted_content_owner,
    }


def _group_sync_plan_to_api(
    plan: GroupSyncPlan, *, dry_run: bool, content_owner_id: str
) -> dict[str, object]:
    """Render a sync plan as the API response body (identical for both modes)."""
    return {
        "dry_run": dry_run,
        "content_owner_id": content_owner_id,
        "counts": dict(plan.counts),
        "unknown_channel_total": plan.unknown_channel_total,
        "non_channel_member_count": plan.non_channel_member_count,
        "groups": [
            {
                "cms_group_id": entry.cms_group_id,
                "outcome": entry.outcome.value,
                "title": entry.title,
                "local_group_id": entry.local_group_id,
                "name_change": list(entry.name_change) if entry.name_change else None,
                "active_change": list(entry.active_change) if entry.active_change else None,
                "members_added": list(entry.members_added),
                "members_removed": list(entry.members_removed),
                "unknown_channel_ids": list(entry.unknown_channel_ids[:50]),
                "unknown_channel_count": len(entry.unknown_channel_ids),
                "will_adopt_content_owner": entry.will_adopt_content_owner,
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
    """Re-parent a channel to a new company, recording CHANNEL_UPDATED unless it's a no-op."""
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
    """Set or clear a channel's CMS content owner, recording CHANNEL_UPDATED unless it's a no-op."""
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
