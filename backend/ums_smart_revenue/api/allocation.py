"""Read-only account-level deduction allocation endpoint (Phase 4 Spec 2b PR-1).

The commit endpoint (Phase 4 Spec 2b PR-5) is the first allocation WRITE path: a
versioned, audited snapshot of the same gross_revenue_proportional compute. It is
write-only; readers (net-revenue, this module's GET, exports) keep computing live.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channel_account_links import (
    current_channel_account_link_repository,
)
from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.revenue import (
    current_deduction_component_repository,
    current_revenue_fact_repository,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.allocation import AccountAllocationResult
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommittedAllocationIdempotencyConflictError,
    CommittedAllocationLockedMonthError,
    CommittedAllocationValidationError,
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository

router = APIRouter(prefix="/revenue", tags=["account-allocations"])


def current_committed_allocation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyCommittedAllocationRepository:
    """Build the committed-allocation repository bound to the request session."""
    return SqlAlchemyCommittedAllocationRepository(session)


class CommitAllocationRequest(BaseModel):
    """Body for a commit: a client idempotency key + a required reason."""

    idempotency_key: str
    reason: str
    allocation_method: str = "gross_revenue_proportional"


class CommitAllocationResponse(BaseModel):
    """Committed run (with its summary) + the snapshot payload + audit event."""

    run: dict[str, object]
    allocations: list[dict[str, object]]
    unallocated: list[dict[str, object]]
    notes: list[dict[str, object]]
    audit_event: dict[str, object] | None


def _request_fingerprint(*, allocation_method: str, reason: str) -> str:
    """Stable digest of the client-controlled request (month is the lookup scope)."""
    canonical = json.dumps(
        {"allocation_method": allocation_method, "reason": reason}, sort_keys=True
    )
    return hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()


def _run_to_api(run) -> dict[str, object]:  # noqa: ANN001
    """Serialize a committed run header (Decimals as strings)."""
    return {
        "run_id": str(run.id),
        "month": run.month,
        "commit_version": run.commit_version,
        "allocation_method": run.allocation_method,
        "idempotency_key": run.idempotency_key,
        "committed_by": str(run.committed_by),
        "committed_at": run.committed_at.isoformat() if run.committed_at else None,
        "reason": run.reason,
        "summary": {
            "component_count": run.component_count,
            "allocated_component_count": run.allocated_component_count,
            "unallocated_component_count": run.unallocated_component_count,
            "allocated_total_usd": decimal_to_api(run.allocated_total_usd),
            "unallocated_total_usd": decimal_to_api(run.unallocated_total_usd),
            "net_applicable_total_usd": decimal_to_api(run.net_applicable_total_usd),
            "reconciliation_total_usd": decimal_to_api(run.reconciliation_total_usd),
        },
    }


def _require_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise HTTP 403 if the principal lacks the permission for the scope."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _require_valid_month(month: str) -> None:
    """Boundary YYYY-MM validation -> 422 before scope/permission checks."""
    valid = (
        len(month) == 7
        and month[4] == "-"
        and month[:4].isascii()
        and month[:4].isdigit()
        and month[5:].isascii()
        and month[5:].isdigit()
        and 1 <= int(month[5:7]) <= 12
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )


def _result_to_api(result: AccountAllocationResult) -> dict[str, object]:
    """Serialize the allocation result (no secrets, Decimals as strings)."""
    return {
        "month": result.month,
        "allocation_method": result.allocation_method,
        "allocations": [
            {
                "adsense_account_id": ln.adsense_account_id,
                "youtube_channel_id": ln.youtube_channel_id,
                "component_kind": ln.component_kind,
                "source_system": ln.source_system,
                "component_key": ln.component_key,
                "basis_source_kind": ln.basis_source_kind,
                "basis_gross_usd": decimal_to_api(ln.basis_gross_usd),
                "basis_share": decimal_to_api(ln.basis_share),
                "allocated_amount_usd": decimal_to_api(ln.allocated_amount_usd),
                "net_applicable": ln.net_applicable,
            }
            for ln in result.lines
        ],
        "unallocated": [
            {
                "scope_id": iss.scope_id,
                "component_kind": iss.component_kind,
                "component_key": iss.component_key,
                "amount_usd": decimal_to_api(iss.amount_usd),
                "issue_code": iss.issue_code,
                "detail": iss.detail,
            }
            for iss in result.unallocated
        ],
        "notes": [
            {
                "note_code": note.note_code,
                "youtube_channel_id": note.youtube_channel_id,
                "detail": note.detail,
            }
            for note in result.notes
        ],
        "summary": {
            "component_count": result.summary.component_count,
            "allocated_component_count": result.summary.allocated_component_count,
            "unallocated_component_count": result.summary.unallocated_component_count,
            "allocated_total_usd": decimal_to_api(result.summary.allocated_total_usd),
            "unallocated_total_usd": decimal_to_api(result.summary.unallocated_total_usd),
            "net_applicable_total_usd": decimal_to_api(
                result.summary.net_applicable_total_usd
            ),
            "reconciliation_total_usd": decimal_to_api(
                result.summary.reconciliation_total_usd
            ),
        },
    }


# ============================================================================
# Purpose: Read-only month endpoint that allocates ACCOUNT-grain deduction
#   evidence to channels via the verified map (source-aligned raw gross). It
#   reads ACCOUNT-only components (no bank-grain rows fetched), resolves each
#   account's verified channels, builds the source-aligned gross basis, and
#   returns allocations + unallocated blocking issues + a conserved summary.
# Database/ORM: Reads deduction_components, adsense_content_owner_links +
#   content_owner_channel_links (via the map contract), monthly_channel_revenue_facts.
# Standards: thin route; 422 on malformed month before scope checks; fail-closed
#   permission gate; sensitive read audit (REVENUE_VIEWED + PAYMENT_VIEWED); no
#   secrets in responses/audit details.
# Blast Radius: Finance read only. No mutation, no migration, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/allocation.py -> pure builder.
#   - File: Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
# ============================================================================
@router.get("/months/{month}/account-allocations")
def get_account_allocations(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    deduction_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    adsense_account_id: Annotated[str | None, Query(min_length=1)] = None,
) -> dict[str, object]:
    """Allocate ACCOUNT-grain deduction evidence to channels for one month."""
    _require_valid_month(month)
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)

    # _require_valid_month is the single 422 boundary gate; the same month is
    # passed to the orchestrator, so repo-level month validation is unreachable.
    result = compute_month_account_allocation(
        month=month,
        deduction_repository=deduction_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        adsense_account_id=adsense_account_id,
    )

    details = {
        "month": month,
        "adsense_account_id": adsense_account_id,
        "allocated_line_count": len(result.lines),
        "unallocated_count": len(result.unallocated),
    }
    audit_events = [
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.REVENUE_VIEWED,
                entity_type="monthly_account_allocations", entity_id=month,
                scope=revenue_scope, details=details,
            )
        ),
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.PAYMENT_VIEWED,
                entity_type="monthly_account_allocations", entity_id=month,
                scope=payment_scope, details=details,
            )
        ),
    ]

    payload = _result_to_api(result)
    payload["audit_events"] = audit_events
    return payload


# ============================================================================
# Purpose: Commit a versioned, audited snapshot of the month's account
#   allocation (gross_revenue_proportional). Re-runs the same live compute under
#   the finance-month advisory lock and persists the result; an idempotent
#   replay returns the existing run without a second audit. This is a WRITE path
#   that drives NO reader number (net-revenue/exports keep computing live).
# Database/ORM: writes committed_allocation_runs/_lines/_unallocated/_notes via
#   the committed-allocation repository; reads deduction_components, the verified
#   account->channel map, monthly_channel_revenue_facts for the compute.
# Standards: thin route; 422 on malformed month before scope checks; fail-closed
#   three-gate permission check (VIEW_REVENUE@global, VIEW_FINALIZED_PAYMENTS +
#   CHANGE_ALLOCATION_RULE@finance_month); typed errors -> 422/409; sensitive
#   summary-only audit (no per-line dump in API or audit detail); no secrets.
# Blast Radius: Finance write; first allocation persistence. Reuses existing
#   read gates; no reader change (regression-proven). No Neo4j impact.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> writer.
#   - File: Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md
# ============================================================================
@router.post(
    "/months/{month}/account-allocations/commit",
    response_model=CommitAllocationResponse,
)
def commit_account_allocations(
    month: str,
    payload: CommitAllocationRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    deduction_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    response: Response,
) -> CommitAllocationResponse:
    """Commit a versioned snapshot of the month's account allocation."""
    _require_valid_month(month)
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, payment_scope)

    fingerprint = _request_fingerprint(
        allocation_method=payload.allocation_method, reason=payload.reason
    )
    try:
        outcome = committed_repository.commit_allocation(
            month=month, allocation_method=payload.allocation_method,
            idempotency_key=payload.idempotency_key, request_fingerprint=fingerprint,
            reason=payload.reason, committed_by=user.user_id,  # str; repo -> UUID
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository, link_repository=link_repository,
        )
    except CommittedAllocationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except (
        CommittedAllocationLockedMonthError,
        CommittedAllocationIdempotencyConflictError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit_event: dict[str, object] | None = None
    if outcome.created:
        audit_event = audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.ALLOCATION_COMMITTED,
                entity_type="committed_allocation_run", entity_id=str(outcome.run.id),
                scope=payment_scope, reason=payload.reason,
                details={
                    "run_id": str(outcome.run.id),
                    "commit_version": outcome.run.commit_version,
                    "month": month,
                    "allocation_method": outcome.run.allocation_method,
                    "component_count": outcome.run.component_count,
                    "allocated_component_count": outcome.run.allocated_component_count,
                    "unallocated_component_count": outcome.run.unallocated_component_count,
                    "allocated_total_usd": decimal_to_api(outcome.run.allocated_total_usd),
                    "net_applicable_total_usd": decimal_to_api(
                        outcome.run.net_applicable_total_usd
                    ),
                    "note_count": len(outcome.notes),
                },
            )
        )
        response.status_code = status.HTTP_201_CREATED

    return CommitAllocationResponse(
        run=_run_to_api(outcome.run),
        allocations=[
            {
                "adsense_account_id": ln.adsense_account_id,
                "youtube_channel_id": ln.youtube_channel_id,
                "component_kind": ln.component_kind, "source_system": ln.source_system,
                "component_key": ln.component_key, "basis_source_kind": ln.basis_source_kind,
                "basis_gross_usd": decimal_to_api(ln.basis_gross_usd),
                "basis_share": decimal_to_api(ln.basis_share),
                "allocated_amount_usd": decimal_to_api(ln.allocated_amount_usd),
                "net_applicable": ln.net_applicable,
            }
            for ln in outcome.lines
        ],
        unallocated=[
            {
                "scope_id": iss.scope_id, "component_kind": iss.component_kind,
                "component_key": iss.component_key,
                "amount_usd": decimal_to_api(iss.amount_usd),
                "issue_code": iss.issue_code, "detail": iss.detail,
            }
            for iss in outcome.unallocated
        ],
        notes=[
            {"note_code": n.note_code, "youtube_channel_id": n.youtube_channel_id,
             "detail": n.detail}
            for n in outcome.notes
        ],
        audit_event=audit_event,
    )
