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
from ums_smart_revenue.finance.channel_account_links import (
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


def _require_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise HTTP 403 if the principal lacks the permission for the scope."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
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
                sink=audit_sink, actor=user,
                event_type=AuditEventType.REVENUE_VIEWED,
                entity_type="channel_account_links", entity_id="list",
                scope=global_scope, details=details,
            )
        ),
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.PAYMENT_VIEWED,
                entity_type="channel_account_links", entity_id="list",
                scope=global_scope, details=details,
            )
        ),
    ]
    has_more = offset + len(page.links) < page.total_count
    return AccountOwnerLinksListResponse(
        total_count=page.total_count,
        returned_count=len(page.links),
        links=[link.to_api() for link in page.links],
        pagination={
            "limit": limit, "offset": offset,
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
        "adsense_account_id", "content_owner_id", "provenance_kind", "reason",
        mode="before",
    )
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


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
    record = record_audit_event(
        sink=audit_sink, actor=user,
        event_type=AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED,
        entity_type="adsense_content_owner_link", entity_id=link.id,
        scope=AccessScope.global_scope(), reason=payload.reason,
        details={"adsense_account_id": link.adsense_account_id,
                 "content_owner_id": link.content_owner_id},
    )
    return AccountOwnerLinkMutationResponse(
        link=link.to_api(), audit_event=audit_record_to_api(record)
    )
