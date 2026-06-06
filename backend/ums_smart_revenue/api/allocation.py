"""Read-only account-level deduction allocation endpoint (Phase 4 Spec 2b PR-1).

The commit endpoint (Phase 4 Spec 2b PR-5) is the first allocation WRITE path: a
versioned, audited snapshot of the same gross_revenue_proportional compute. Phase 4
Spec 2b PR-6 (read-switch) then makes this module's GET, net-revenue, explain, and
exports prefer that committed snapshot for LOCKED months (lock-aware; OPEN stays
live) via resolve_month_account_allocation.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
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
    current_committed_allocation_repository,
    current_deduction_component_repository,
    current_org_access_index,
    current_revenue_fact_repository,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.finance.account_allocation_read import (
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.allocation import AccountAllocationResult
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


class CommitAllocationRequest(BaseModel):
    """Body for a commit: a client idempotency key + a required reason."""

    idempotency_key: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    allocation_method: str = "gross_revenue_proportional"

    @field_validator("idempotency_key", "reason", mode="before")
    @classmethod
    def _strip(cls, value):
        """Strip whitespace so a blank/whitespace-only value fails min_length=1.

        Pydantic rejects the stripped-to-empty result at the boundary (422)
        before any DB write or audit call — the route contract promised 422,
        not the IntegrityError/ValueError 500s a bare ``str`` produced.
        """
        return value.strip() if isinstance(value, str) else value


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
                "basis_amount_usd": decimal_to_api(ln.basis_amount_usd),
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
# Purpose: Read-only month endpoint returning the ACCOUNT-grain deduction
#   allocation to channels. Via the read-switch resolver: a LOCKED month with a
#   committed snapshot returns it (gross OR post_tax, reconstructed); otherwise it
#   computes live (source-aligned gross). It reads ACCOUNT-only components (no
#   bank-grain rows fetched), resolves each account's verified channels, and
#   returns allocations + unallocated blocking issues + a conserved summary +
#   allocation-source provenance.
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
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    session: Annotated[Session, Depends(current_db_session)],
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
    # passed to the resolver, so repo-level month validation is unreachable. The
    # read-switch resolver prefers the committed snapshot for a LOCKED month
    # (live_fallback if none committed); OPEN / no close row stays live_compute.
    result, allocation_provenance = resolve_month_account_allocation(
        month=month,
        session=session,
        deduction_repository=deduction_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        committed_repository=committed_repository,
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
    payload.update(allocation_provenance_to_api(allocation_provenance))
    payload["audit_events"] = audit_events
    return payload


# ============================================================================
# Purpose: Commit a versioned, audited snapshot of the month's account
#   allocation (gross / post-tax / company_level / no_allocation, allowlisted).
#   Re-runs the same live compute under the finance-month advisory lock and
#   persists the result; an idempotent replay returns the existing run without a
#   second audit. company_level consumes the org-access channel->company map
#   (read-only, resolved here at the boundary); no_allocation snapshots its full
#   intentional-withhold set. This is the WRITE path; readers prefer the
#   committed snapshot for LOCKED months via the PR-6 read-switch.
# Database/ORM: writes committed_allocation_runs/_lines/_unallocated/_notes via
#   the committed-allocation repository; reads deduction_components, the verified
#   account->channel map, monthly_channel_revenue_facts, and the org-unit
#   channel->company index for the compute.
# Standards: thin route; 422 on malformed month before scope checks; fail-closed
#   three-gate permission check (VIEW_REVENUE@global, VIEW_FINALIZED_PAYMENTS +
#   CHANGE_ALLOCATION_RULE@finance_month); typed errors -> 422/409; sensitive
#   summary-only audit (no per-line dump in API or audit detail); no secrets.
# Blast Radius: Finance write; first allocation persistence. Reuses existing
#   read gates; OPEN-month reads still compute live (PR-5 regression-proven),
#   LOCKED-month reads consume this snapshot (PR-6 read-switch). No Neo4j impact.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> writer.
#   - File: Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md
# ============================================================================
@router.post(
    "/months/{month}/account-allocations/commit",
    response_model=CommitAllocationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": CommitAllocationResponse,
            "description": (
                "Idempotent replay: the existing committed run for this "
                "(month, idempotency_key) is returned unchanged and NO new "
                "ALLOCATION_COMMITTED audit event is recorded."
            ),
        },
        status.HTTP_201_CREATED: {
            "description": (
                "A new versioned snapshot was committed and an "
                "ALLOCATION_COMMITTED audit event was recorded."
            ),
        },
    },
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
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
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
            channel_company=org_index.channel_company,
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
    # The decorator declares 201 as the default success status. A fresh commit
    # keeps 201 (new snapshot + audit); an idempotent replay returns the existing
    # run with 200 and no second audit -- matching the documented OpenAPI responses.
    response.status_code = (
        status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    )

    return CommitAllocationResponse(
        run=_run_to_api(outcome.run),
        allocations=[
            {
                "adsense_account_id": ln.adsense_account_id,
                "youtube_channel_id": ln.youtube_channel_id,
                "component_kind": ln.component_kind, "source_system": ln.source_system,
                "component_key": ln.component_key, "basis_source_kind": ln.basis_source_kind,
                "basis_amount_usd": decimal_to_api(ln.basis_amount_usd),
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
