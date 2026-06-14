"""Read/write API for the channel↔account map (Phase 4 Spec 2a).

Thin routes: parse input, resolve the repository, enforce boundary permissions,
call the repository, translate typed errors, and record sensitive audit events.
provenance_payload is never serialized. Audit persistence reuses the shared
``current_audit_sink`` (create_app overrides it to a SQL sink).

NOTE — built across Tasks 11–13; imports accrete. Add each symbol in the task
that first uses it so every commit stays ruff-clean:
  Task 12 → `Field, field_validator` from pydantic (request model).
  Task 13 → `ChannelAccountLinkConflictError, ChannelAccountLinkNotFoundError`
            from finance.channel_account_links.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import MalformedAdsenseAccountIdError
from ums_smart_revenue.finance.channel_account_links import (
    ChannelAccountLinkConflictError,
    ChannelAccountLinkLockedMonthError,
    ChannelAccountLinkNotFoundError,
    ChannelAccountLinkValidationError,
    SqlAlchemyChannelAccountLinkRepository,
)

router = APIRouter(prefix="/revenue", tags=["channel-account-links"])


def current_channel_account_link_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelAccountLinkRepository:
    """Build the tenant-aware channel-account-link repository for a request."""
    return SqlAlchemyChannelAccountLinkRepository(session)


class AccountOwnerLinksListResponse(BaseModel):
    """Typed list response for account↔owner links (no provenance_payload)."""

    total_count: int
    returned_count: int
    links: list[dict[str, object]]
    pagination: dict[str, object]
    audit_events: list[dict[str, object]]


def _require_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    """Raise HTTP 403 if the principal lacks the permission for the scope."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _iter_months(start: str, end: str) -> list[str]:
    """Return each YYYY-MM month from start to end inclusive (assumes start <= end)."""
    year, month = int(start[:4]), int(start[5:7])
    end_year, end_month = int(end[:4]), int(end[5:7])
    months: list[str] = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


# ============================================================================
# Purpose: Authorize a verify/reject decision over the link's FULL effective
#   month range, not just its start month, before the money-gating transition.
# Database/ORM: None (pure authorization predicate).
# Standards: fail-closed; HTTP 403 via _require_permission; route-boundary authz.
# Blast Radius: Authorization (tightens; never broadens). No finance/audit write.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission scope match.
#   - File: backend/ums_smart_revenue/finance/channel_account_links.py -> a
#     VERIFIED link is consumed by allocation for every month in [start, end].
# ============================================================================
def _require_allocation_permission_for_range(
    user: UserPrincipal, start: str, end: str | None
) -> None:
    """Require CHANGE_ALLOCATION_RULE for every month a verified link is consumed.

    A verified account↔owner link feeds allocation for every month in its
    [start, end] effective range, so a caller scoped to only the start month
    must not be able to approve (or reject) a mapping that changes allocations
    in later months they were not granted. An open-ended link (end=None) spans
    an unbounded set of future months that no finite set of month-scoped grants
    can cover, so only a global allocation grant authorizes it. A caller holding
    the grant at GLOBAL scope authorizes every covered month, so the bounded path
    short-circuits that case with one check; otherwise it checks each month
    (falling back to the start month if the stored range is empty, so it never
    authorizes a non-global caller without at least one explicit month check).
    """
    if end is None:
        _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope())
        return
    # FIX (PR #57 N10): a CHANGE_ALLOCATION_RULE grant at global scope authorizes
    # every finance month (OrgAccessIndex.contains returns True for a GLOBAL
    # granted scope against any target), so it is a strict superset of the
    # per-month checks below. Short-circuit it once to avoid ~95k in-memory authz
    # iterations for an authorized far-future bounded effective_month_end (e.g.
    # 9999-12). Use the NON-raising has_permission so a non-global caller falls
    # through to the per-month loop and is still gated month-by-month.
    if has_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope()):
        return
    for month in _iter_months(start, end) or [start]:
        _require_permission(
            user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(month)
        )


# ============================================================================
# Purpose: List account↔owner links (global-scoped management view). The
#   AdSense account id is finalized-payment context, so the gate requires both
#   VIEW_REVENUE and VIEW_FINALIZED_PAYMENTS at global scope.
# Database/ORM: adsense_content_owner_links (read-only).
# Standards: thin route; typed 422 on malformed month; provenance_payload never
#   serialized; sensitive audit (REVENUE_VIEWED + PAYMENT_VIEWED).
# Blast Radius: Authorization (fail-closed add); audit. No finance mutation.
# ============================================================================
@router.get("/channel-account-links", response_model=AccountOwnerLinksListResponse)
def list_channel_account_links(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    status_filter: Annotated[str | None, Query(alias="status", min_length=1)] = None,
    adsense_account_id: Annotated[str | None, Query(min_length=1)] = None,
    content_owner_id: Annotated[str | None, Query(min_length=1)] = None,
    month: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AccountOwnerLinksListResponse:
    """List account↔owner links for operator review (global-scoped)."""
    global_scope = AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, global_scope)
    try:
        page = repository.list_account_owner_links(
            status=status_filter,
            adsense_account_id=adsense_account_id,
            content_owner_id=content_owner_id,
            month=month,
            limit=limit,
            offset=offset,
        )
    except ChannelAccountLinkValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    details = {"total_count": page.total_count, "returned_count": len(page.links)}
    audit_events = [
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink,
                actor=user,
                event_type=AuditEventType.REVENUE_VIEWED,
                entity_type="channel_account_links",
                entity_id="list",
                scope=global_scope,
                details=details,
            )
        ),
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink,
                actor=user,
                event_type=AuditEventType.PAYMENT_VIEWED,
                entity_type="channel_account_links",
                entity_id="list",
                scope=global_scope,
                details=details,
            )
        ),
    ]
    has_more = offset + len(page.links) < page.total_count
    return AccountOwnerLinksListResponse(
        total_count=page.total_count,
        returned_count=len(page.links),
        links=[link.to_api() for link in page.links],
        pagination={
            "limit": limit,
            "offset": offset,
            "next_offset": (offset + limit) if has_more else None,
            "has_more": has_more,
        },
        audit_events=audit_events,
    )


class ProposeAccountOwnerLinkRequest(BaseModel):
    """Validated payload to propose an UNVERIFIED account↔owner link."""

    adsense_account_id: str = Field(min_length=1)
    content_owner_id: str = Field(min_length=1)
    effective_month_start: str = Field(min_length=7, max_length=7)
    effective_month_end: str | None = None
    provenance_kind: str = Field(min_length=1)
    provenance_payload: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(min_length=1)

    @field_validator(
        "content_owner_id",
        "provenance_kind",
        "reason",
        mode="before",
    )
    @classmethod
    def _strip(cls, value):
        """Strip leading/trailing whitespace from string field values."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("adsense_account_id", mode="before")
    @classmethod
    def _canonicalize_adsense_account_id(cls, value):
        """Strip whitespace + the accounts/ prefix; reject malformed account ids."""
        if not isinstance(value, str):
            return value
        try:
            # FIX: strip here rather than relying on the shared `_strip`
            # validator. Pydantic v2 runs same-mode `before` validators in
            # reverse declaration order, so `_canonicalize` ran BEFORE `_strip`
            # and passed the still-padded value to `_validated_account_id`,
            # which fail-closes on surrounding whitespace — so a valid padded id
            # like "  accounts/pub-1  " was rejected with 422. adsense_account_id
            # is no longer in `_strip`'s field list to avoid double-processing.
            return _validated_account_id(value.strip())
        except MalformedAdsenseAccountIdError as exc:
            raise ValueError(str(exc)) from exc


class AccountOwnerLinkMutationResponse(BaseModel):
    """Typed response for a single-link mutation."""

    link: dict[str, object]
    audit_event: dict[str, object]


# ============================================================================
# Purpose: Propose an UNVERIFIED account↔owner link. A proposal is a mapping
#   assertion (not money-affecting until verified), gated by MANAGE_ORG_MAPPING.
# Database/ORM: adsense_content_owner_links (insert).
# Standards: thin route; 422 on malformed month; reason-required audit; no
#   provenance_payload in the response.
# Blast Radius: Authorization (fail-closed); audit; finance map write.
# ============================================================================
@router.post(
    "/channel-account-links",
    response_model=AccountOwnerLinkMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose_channel_account_link(
    payload: ProposeAccountOwnerLinkRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Propose an UNVERIFIED account↔owner link (operator-asserted)."""
    _require_permission(user, Permission.MANAGE_ORG_MAPPING, AccessScope.global_scope())
    try:
        link = repository.propose_account_owner_link(
            adsense_account_id=payload.adsense_account_id,
            content_owner_id=payload.content_owner_id,
            effective_month_start=payload.effective_month_start,
            effective_month_end=payload.effective_month_end,
            provenance_kind=payload.provenance_kind,
            provenance_payload=payload.provenance_payload,
        )
    except ChannelAccountLinkValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ChannelAccountLinkConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED,
        entity_type="adsense_content_owner_link",
        entity_id=link.id,
        scope=AccessScope.global_scope(),
        reason=payload.reason,
        details={
            "adsense_account_id": link.adsense_account_id,
            "content_owner_id": link.content_owner_id,
        },
    )
    return AccountOwnerLinkMutationResponse(
        link=link.to_api(), audit_event=audit_record_to_api(record)
    )


class LinkDecisionRequest(BaseModel):
    """Validated payload for a verify/reject decision (reason required)."""

    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value):
        """Strip leading/trailing whitespace from the reason string."""
        return value.strip() if isinstance(value, str) else value


def _decide_link(
    *,
    link_id: str,
    reason: str,
    verify: bool,
    user: UserPrincipal,
    repository: SqlAlchemyChannelAccountLinkRepository,
    audit_sink: AuditSink,
) -> AccountOwnerLinkMutationResponse:
    """Shared verify/reject handler: gate, exact-load, authorize on month, mutate, audit.

    MANAGE_ORG_MAPPING (global, month-independent) is checked FIRST so a caller
    without org-mapping trust cannot probe link existence. The exact link is then
    loaded by id (404 if unknown — no list pagination), and CHANGE_ALLOCATION_RULE
    is checked on EVERY finance month in the link's effective range (not just the
    start month) so a month-scoped caller cannot approve a multi-month mapping.
    """
    _require_permission(user, Permission.MANAGE_ORG_MAPPING, AccessScope.global_scope())
    try:
        existing = repository.get_account_owner_link(link_id)
    except ChannelAccountLinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown link") from exc
    # FIX: a VERIFIED link is consumed by allocation for every month in its
    # [start, end] range; the previous check only gated effective_month_start,
    # letting a caller scoped to the start month approve later months they were
    # not granted (and any month for an open-ended range). Gate the full range.
    _require_allocation_permission_for_range(
        user, existing.effective_month_start, existing.effective_month_end
    )
    # FIX: user.user_id is a str on UserPrincipal; the repository's verified_by
    # parameter is typed UUID and the ORM column is Uuid() — pass a UUID object
    # so SQLite's pysqlite dialect can call .hex without AttributeError.
    try:
        actor_uuid = UUID(user.user_id)
    except ValueError as exc:
        # FIX: a malformed principal id (headers-authz mode does not normalize
        # X-User-Id) must fail clean, not surface as an uncaught 500 after auth.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid actor identity",
        ) from exc
    try:
        if verify:
            link = repository.verify_account_owner_link(
                link_id, verified_by=actor_uuid, reason=reason
            )
            event_type = AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED
        else:
            link = repository.reject_account_owner_link(
                link_id, verified_by=actor_uuid, reason=reason
            )
            event_type = AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED
    except ChannelAccountLinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ChannelAccountLinkLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ChannelAccountLinkConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # FIX: a verify/reject changes allocation eligibility for EVERY month in the
    # link's [effective_month_start, effective_month_end] range, but the audit
    # scope is a single finance month (start). Record the full affected range in
    # details so month-level audit review of a later (now closed) period can still
    # surface the mutation that touched it, not only the start-month event.
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=event_type,
        entity_type="adsense_content_owner_link",
        entity_id=link.id,
        scope=AccessScope.finance_month(link.effective_month_start),
        reason=reason,
        details={
            "adsense_account_id": link.adsense_account_id,
            "verification_status": link.verification_status,
            "effective_month_start": link.effective_month_start,
            "effective_month_end": link.effective_month_end,
        },
    )
    return AccountOwnerLinkMutationResponse(
        link=link.to_api(), audit_event=audit_record_to_api(record)
    )


# ============================================================================
# Purpose: Verify/reject an account↔owner link — the money-gating trust
#   decision. Requires BOTH MANAGE_ORG_MAPPING (global) and CHANGE_ALLOCATION_
#   RULE (finance month of the link's start). Verify enforces the overlap
#   invariant (409). reason-required sensitive audit.
# Database/ORM: adsense_content_owner_links (update).
# Blast Radius: Authorization (fail-closed, dual); audit; finance map state.
# ============================================================================
@router.post(
    "/channel-account-links/{link_id}/verify",
    response_model=AccountOwnerLinkMutationResponse,
)
def verify_channel_account_link(
    link_id: str,
    payload: LinkDecisionRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Verify an account↔owner link (dual-permission, overlap-guarded)."""
    return _decide_link(
        link_id=link_id,
        reason=payload.reason,
        verify=True,
        user=user,
        repository=repository,
        audit_sink=audit_sink,
    )


@router.post(
    "/channel-account-links/{link_id}/reject",
    response_model=AccountOwnerLinkMutationResponse,
)
def reject_channel_account_link(
    link_id: str,
    payload: LinkDecisionRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Reject an account↔owner link (dual-permission)."""
    return _decide_link(
        link_id=link_id,
        reason=payload.reason,
        verify=False,
        user=user,
        repository=repository,
        audit_sink=audit_sink,
    )
