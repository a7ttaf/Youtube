import re
from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
    current_principal_from_headers,
)

# FIX: Import the canonical dependency providers from dependencies_finance so
# that all callers (this module, allocation.py, channels.py, groups.py,
# exports.py) share a single Python function object — FastAPI dependency_overrides
# keying on the object here is the same key as any `from api.revenue import`
# or `from api.dependencies_finance import` in tests.
from ums_smart_revenue.api.dependencies_finance import (
    current_channel_account_link_repository,
    current_committed_allocation_repository,
    current_deduction_component_repository,
    current_org_access_index,
    current_revenue_fact_repository,
)
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
from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.account_allocation_read import (
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.allocation import AllocationLine
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationLockedMonthError,
    BankReconciliationValidationError,
    SqlAlchemyBankReconciliationRepository,
    build_month_bank_reconciliation_summary,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommittedAllocationIdempotencyConflictError,
    CommittedAllocationLockedMonthError,
    CommittedAllocationValidationError,
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.explanations import (
    NET_REVENUE_METRIC,
    REVENUE_RECONCILIATION_METRIC,
    SUPPORTED_METRICS,
    NumberExplanationValidationError,
    SqlAlchemyNumberExplanationRepository,
    build_channel_month_revenue_explanation,
)
from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideConflictError,
    ManualOverrideLockedMonthError,
    ManualOverrideNotFoundError,
    ManualOverrideValidationError,
    RevenueManualOverrideEntry,
    SqlAlchemyManualOverrideRepository,
)
from ums_smart_revenue.finance.month_close import SqlAlchemyFinanceMonthCloseRepository
from ums_smart_revenue.finance.net_revenue import (
    NET_APPLICABLE_COMPONENT_KINDS,
    NetRevenueValidationError,
    build_month_net_revenue_summary,
    filter_account_allocations_to_scope,
    normalize_net_revenue_currency,
)
from ums_smart_revenue.finance.payment_matching import (
    PaymentMatchValidationError,
    build_monthly_payment_match_summary,
    normalize_payment_match_currency,
)
from ums_smart_revenue.finance.recalculation import (
    RevenueRecalculationValidationError,
    build_recalculation_preview,
    normalize_allocation_method,
)
from ums_smart_revenue.finance.reconciliation import (
    build_revenue_reconciliation_issue_queue,
    build_revenue_reconciliation_preview,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    RevenueFactLockedMonthError,
    RevenueFactNotFoundError,
    RevenueFactSourceKind,
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary
from ums_smart_revenue.finance.smart_alerts import (
    build_monthly_smart_alert_summary,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

router = APIRouter(prefix="/revenue", tags=["revenue"])
MONTH_VALUE_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_AUDIT_SINK = InMemoryAuditSink()
_REVENUE_SOURCE_KINDS_BY_CONNECTOR_KEY = {
    "youtube-cms": {RevenueFactSourceKind.YOUTUBE_CMS.value},
    "youtube_reporting": {RevenueFactSourceKind.YOUTUBE_CMS.value},
    "youtube-analytics": {RevenueFactSourceKind.YOUTUBE_ANALYTICS.value},
    "youtube_analytics": {RevenueFactSourceKind.YOUTUBE_ANALYTICS.value},
    "adsense": {RevenueFactSourceKind.ADSENSE.value},
    "manual-upload": {RevenueFactSourceKind.MANUAL_UPLOAD.value},
    "manual_upload": {RevenueFactSourceKind.MANUAL_UPLOAD.value},
    "allocation": {RevenueFactSourceKind.ALLOCATION.value},
}


class AuthorizationCheckResponse(BaseModel):
    """API response confirming whether a principal holds revenue access for a channel."""

    authorized: bool
    channel_id: str
    permission: str


class DeductionComponentApiItem(BaseModel):
    """API-safe deduction-component item without raw source payload."""

    id: str
    month: str
    component_kind: str
    scope_kind: str
    scope_id: str
    amount_usd: str
    amount_native: str | None
    currency_code: str
    source_system: str
    source_table: str
    source_id: str | None
    source_key: str | None
    source_report_id: str | None
    component_key: str


class DeductionComponentScopeGroup(BaseModel):
    """One full-match scope aggregate plus paginated component rows."""

    scope_kind: str
    component_count: int
    total_amount_usd: str
    components: list[DeductionComponentApiItem]


class DeductionComponentsPagination(BaseModel):
    """Offset pagination metadata for deduction-component reads."""

    limit: int
    offset: int
    next_offset: int | None
    has_more: bool


class AuditEventResponse(BaseModel):
    """Safe audit-event shape returned by finance read endpoints."""

    event_type: str
    entity_type: str
    entity_id: str
    scope_type: str
    scope_id: str | None
    reason: str | None
    sensitive: bool


class MonthDeductionComponentsResponse(BaseModel):
    """Typed monthly deduction-components response."""

    month: str
    total_count: int
    returned_count: int
    scopes: list[DeductionComponentScopeGroup]
    pagination: DeductionComponentsPagination
    audit_events: list[AuditEventResponse]


class RevenueFactImportRequest(BaseModel):
    """Validated request payload for importing a connector-sourced monthly revenue fact."""

    month: str
    youtube_channel_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    connector_key: str = Field(min_length=1)
    source_report_id: str | None = None
    gross_revenue_usd: Decimal = Field(ge=0)
    net_revenue_usd: Decimal | None = Field(default=None, ge=0)
    shorts_revenue_usd: Decimal | None = Field(default=None, ge=0)
    longform_revenue_usd: Decimal | None = Field(default=None, ge=0)
    subscription_revenue_usd: Decimal | None = Field(default=None, ge=0)
    views: int = Field(default=0, ge=0)
    watch_time_minutes: Decimal = Field(default=Decimal("0"), ge=0)
    confidence_score: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    reason: str = Field(min_length=1)

    @field_validator(
        "month",
        "youtube_channel_id",
        "source_kind",
        "connector_key",
        "reason",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace from required string fields and reject blank values."""
        return _strip_required_string(value)

    @field_validator("source_report_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Strip whitespace from optional string fields, returning None for blank input."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class ManualOverrideCreateRequest(BaseModel):
    """Validated request payload for creating a pending manual revenue override."""

    month: str
    youtube_channel_id: str = Field(min_length=1)
    adjustment_revenue_usd: Decimal
    reason: str = Field(min_length=1)

    @field_validator("month", "youtube_channel_id", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace from required string fields and reject blank values."""
        return _strip_required_string(value)


class ManualOverrideApprovalRequest(BaseModel):
    """Validated request payload for approving an existing manual revenue override."""

    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace from required string fields and reject blank values."""
        return _strip_required_string(value)


class BankReconciliationRecordRequest(BaseModel):
    """Validated request payload for recording a bank-received reconciliation entry."""

    bank_reference: str = Field(min_length=1)
    bank_received_date: date
    bank_received_amount: Decimal = Field(ge=0)
    bank_received_currency: str = Field(min_length=1)
    bank_received_amount_usd: Decimal = Field(ge=0)
    transfer_fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    fx_difference_usd: Decimal = Decimal("0")
    notes: str | None = None
    source_report_id: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("bank_reference", "bank_received_currency", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace from required string fields and reject blank values."""
        return _strip_required_string(value)

    @field_validator("notes", "source_report_id", mode="before")
    @classmethod
    def strip_optional_strings(cls, value):
        """Strip whitespace from optional string fields, returning None for blank input."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class RevenueRecalculationRequest(BaseModel):
    """Validated request payload for triggering a scoped revenue recalculation preview."""

    month: str
    allocation_method: str = Field(min_length=1)
    scope_type: str = Field(default="global", min_length=1)
    scope_id: str | None = None
    currency: str = Field(default="USD", min_length=1)
    dry_run: bool = True
    # Required only when dry_run is False (the route enforces it after the gates);
    # ignored on a dry run. Shared with the commit endpoint for cross-endpoint
    # idempotency coherence via commit_request_fingerprint.
    idempotency_key: str | None = None
    reason: str = Field(min_length=1)

    @field_validator(
        "month",
        "allocation_method",
        "scope_type",
        "currency",
        "reason",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace from required string fields and reject blank values."""
        return _strip_required_string(value)

    @field_validator("scope_id", "idempotency_key", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Strip whitespace from optional string fields, returning None for blank input."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_adsense_payment_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyAdSensePaymentRepository:
    """Build the AdSense payment repository bound to the current database session."""
    return SqlAlchemyAdSensePaymentRepository(session)


def current_manual_override_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyManualOverrideRepository:
    """Build the manual-override repository bound to the current database session."""
    return SqlAlchemyManualOverrideRepository(session)


def current_bank_reconciliation_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyBankReconciliationRepository:
    """Build the bank-reconciliation repository bound to the current database session."""
    return SqlAlchemyBankReconciliationRepository(session)


def current_finance_month_close_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyFinanceMonthCloseRepository:
    """Build the finance-month-close repository bound to the current database session."""
    return SqlAlchemyFinanceMonthCloseRepository(session)


def current_number_explanation_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyNumberExplanationRepository:
    """Build the tenant-aware number-explanation repository for a request."""
    return SqlAlchemyNumberExplanationRepository(session)


def current_revenue_audit_sink() -> InMemoryAuditSink:
    """Return the module-level in-memory audit sink for revenue route events."""
    return _AUDIT_SINK


def sql_revenue_audit_sink_from_session(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyAuditSink:
    """Build a SQLAlchemy-backed audit sink bound to the current database session."""
    return SqlAlchemyAuditSink(session)


@router.get("/channels/{channel_id}/authorization-check", response_model=AuthorizationCheckResponse)
def check_channel_revenue_authorization(
    channel_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> AuthorizationCheckResponse:
    """Check whether the caller holds VIEW_REVENUE permission for the given channel."""
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    return AuthorizationCheckResponse(
        authorized=True,
        channel_id=channel_id,
        permission=Permission.VIEW_REVENUE.value,
    )


# ============================================================================
# Purpose: Enforce the dry_run=False request-shape contract that is stricter
#   than a dry run: a committed write must be whole-month (scope_type=global),
#   carry an idempotency_key, and never run the manual method (manual needs the
#   explicit-lines commit endpoint). Returns the NORMALIZED allocation method for
#   the downstream commit so the manual check and the service agree on casing.
# Database/ORM: None.
# Standards: typed 422s with safe, actionable messages; runs AFTER the auth gates
#   (authorization-before-validation parity with the commit endpoint).
# Blast Radius: Authorization order + write-path validation. No finance number.
# ============================================================================
def _validate_recalculation_write_request(payload: RevenueRecalculationRequest) -> str:
    """Validate the dry_run=False request shape; return the normalized method."""
    if payload.scope_type.strip() != "global":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="committed recalculation requires scope_type=global",
        )
    if not payload.idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="idempotency_key is required when dry_run=false",
        )
    try:
        normalized_method = normalize_allocation_method(payload.allocation_method)
    except RevenueRecalculationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if normalized_method == "manual":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "manual allocation requires explicit lines; use POST "
                "/revenue/months/{month}/account-allocations/commit"
            ),
        )
    return normalized_method


# ============================================================================
# Purpose: Perform the committed recalculation write after the preview pre-flight
#   gate passed. Delegates to the SAME committed-allocation service as the commit
#   endpoint (advisory lock, idempotency, version chain), reusing the shared
#   commit_request_fingerprint for cross-endpoint idempotency coherence, then
#   emits the durable ALLOCATION_COMMITTED audit. Sets the response status to 201
#   (fresh) or 200 (idempotent replay) and returns the run/audit fragment merged
#   into the preview body by the route.
# Database/ORM: writes committed_allocation_runs/_lines/_unallocated/_notes via
#   the committed-allocation repository; writes one ALLOCATION_COMMITTED row on a
#   fresh commit via the durable audit sink.
# Standards: typed service errors -> 422/409 (parity with the commit route);
#   summary-only audit; no per-line dump; manual_lines never passed (rejected
#   earlier). Reuses api.allocation helpers (top-level import; cycle resolved by
#   api/dependencies_finance.py).
# Blast Radius: Finance write; identical persistence path to the commit endpoint.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> writer.
#   - File: backend/ums_smart_revenue/api/allocation.py -> shared fingerprint /
#     run serializer / audit emitter (reused, no logic drift).
# ============================================================================
def _commit_recalculation_write(
    *,
    payload: RevenueRecalculationRequest,
    normalized_method: str,
    user: UserPrincipal,
    org_index: OrgAccessIndex,
    month_scope: AccessScope,
    committed_repository: SqlAlchemyCommittedAllocationRepository,
    deduction_repository: SqlAlchemyDeductionComponentRepository,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    audit_sink: AuditSink,
    response: Response,
) -> dict[str, object]:
    """Commit the recalculation snapshot and return the write-response fragment."""
    # Deferred import: api.allocation imports from api.channels imports from
    # api.revenue, so a top-level import in api.revenue would form a cycle.
    # All three helpers are stable — no logic drift across endpoints.
    from ums_smart_revenue.api.allocation import (
        _run_to_api,
        commit_request_fingerprint,
        emit_allocation_committed_audit,
    )
    fingerprint = commit_request_fingerprint(
        allocation_method=normalized_method, reason=payload.reason,
    )
    try:
        outcome = committed_repository.commit_allocation(
            month=payload.month, allocation_method=normalized_method,
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    write_status = "COMMITTED" if outcome.created else "IDEMPOTENT_REPLAY"
    commit_audit_event = emit_allocation_committed_audit(
        sink=audit_sink, actor=user, month=payload.month, scope=month_scope,
        reason=payload.reason, outcome=outcome,
    )
    response.status_code = (
        status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    )
    return {
        "write_status": write_status,
        "committed_run": _run_to_api(outcome.run),
        "commit_audit_event": commit_audit_event,
    }


# ============================================================================
# Purpose: Preview a scoped revenue recalculation (dry_run=true), or commit a
#   whole-month allocation snapshot (dry_run=false) via the same service path as
#   the dedicated commit endpoint. dry_run=true never writes; dry_run=false adds a
#   stricter contract: scope_type=global, an idempotency_key, the write-only
#   VIEW_FINALIZED_PAYMENTS gate, and a preview pre-flight whose blocking issues
#   return 409 BLOCKED_BY_ISSUES before any write.
# Database/ORM: dry-run reads only; the write branch persists committed allocation
#   rows + one ALLOCATION_COMMITTED audit row (see _commit_recalculation_write).
# Standards: thin route; fail-closed gates BEFORE request-shape validation BEFORE
#   pre-flight BEFORE the service; typed errors -> 422/409; in-memory RECALCULATION
#   audit carries the final write_status. No secrets, no per-line dump.
# Blast Radius: Authorization (added write gate), finance write, audit. No Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/recalculation.py -> pre-flight.
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> writer.
# ============================================================================
@router.post(
    "/recalculate",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Dry-run preview (no writes), or an idempotent replay of an "
                "existing committed run (no second ALLOCATION_COMMITTED audit)."
            ),
        },
        status.HTTP_201_CREATED: {
            "description": (
                "dry_run=false: a new versioned allocation snapshot was committed "
                "and an ALLOCATION_COMMITTED audit event was recorded."
            ),
        },
        status.HTTP_409_CONFLICT: {
            "description": (
                "dry_run=false write conflict, two shapes: a pre-flight "
                "{write_status: BLOCKED_BY_ISSUES, blocking_issues: [...]} dict, "
                "or a plain-string detail (LOCKED month / idempotency-key reused "
                "with a different request)."
            ),
        },
    },
)
def request_revenue_recalculation(
    payload: RevenueRecalculationRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    deduction_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    response: Response,
) -> dict[str, object]:
    """Preview (dry_run) or commit (dry_run=false) a revenue recalculation."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        org_index=org_index,
    )
    month_scope = AccessScope.finance_month(payload.month)
    # Gates first (authorization before request-shape validation). The write path
    # must never be weaker than the commit endpoint: dry_run=false adds the
    # write-only VIEW_FINALIZED_PAYMENTS gate right after the two dry-run gates.
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, month_scope)
    if not payload.dry_run:
        _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)

    normalized_write_method: str | None = None
    if not payload.dry_run:
        normalized_write_method = _validate_recalculation_write_request(payload)

    try:
        facts = revenue_repository.list_month_facts(
            month=payload.month,
            youtube_channel_ids=channel_ids,
        )
        overrides = override_repository.list_month_overrides(
            month=payload.month,
            youtube_channel_ids=channel_ids,
        )
        preview = build_recalculation_preview(
            month=payload.month,
            allocation_method=payload.allocation_method,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            currency=payload.currency,
            dry_run=payload.dry_run,
            facts=facts,
            manual_overrides=overrides,
            # company_level parity: the preview mirrors the commit engine's
            # fail-closed COMPANY_UNMAPPED path from the same org index.
            channel_company=org_index.channel_company,
            # Pass the scoped channel set so verified channels without fact
            # rows are also checked against the company mapping, preventing
            # false READY_FOR_REVIEW before a company_level commit.
            verified_channel_ids=(
                frozenset(channel_ids) if channel_ids is not None else None
            ),
        )
    except (
        ManualOverrideValidationError,
        RevenueFactValidationError,
        RevenueRecalculationValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    write_fragment: dict[str, object] | None = None
    if not payload.dry_run:
        # FIX: check idempotency BEFORE the preflight gate. If a run was already
        # committed under this key the preflight is irrelevant — the request is a
        # replay regardless of what the current facts look like. Without this check
        # a valid committed run would return 409 BLOCKED_BY_ISSUES on retry once
        # facts change (e.g. a net-revenue row is removed), which is wrong because
        # the key/fingerprint already identifies a completed write.
        _existing_run = committed_repository.get_run_by_idempotency_key(
            month=payload.month, idempotency_key=payload.idempotency_key,
        )
        if _existing_run is None:
            # No existing run — enforce the preflight gate before any write.
            if preview.blocking_issues:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "write_status": "BLOCKED_BY_ISSUES",
                        "blocking_issues": [
                            issue.to_api() for issue in preview.blocking_issues
                        ],
                    },
                )
        write_fragment = _commit_recalculation_write(
            payload=payload,
            normalized_method=normalized_write_method,  # set on the write path
            user=user,
            org_index=org_index,
            month_scope=month_scope,
            committed_repository=committed_repository,
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            audit_sink=audit_sink,
            response=response,
        )

    final_write_status = (
        write_fragment["write_status"] if write_fragment else preview.write_status
    )
    summary = preview.source_summary.to_api()
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.RECALCULATION_REQUESTED,
        entity_type="revenue_recalculation_preview",
        entity_id=f"{preview.month}:{preview.allocation_method}",
        scope=month_scope,
        reason=payload.reason,
        details={
            "status": preview.status,
            "allocation_method": preview.allocation_method,
            "scope_type": preview.scope_type,
            "scope_id": preview.scope_id,
            "revenue_fact_count": summary["revenue_fact_count"],
            "source_channel_count": summary["source_channel_count"],
            "write_status": final_write_status,
        },
    )
    response_body = preview.to_api()
    if write_fragment is not None:
        # The route owns the final write_status in the response body; the preview
        # object itself stays NO_WRITES_PERFORMED (it never writes).
        response_body.update(write_fragment)
    response_body["audit_event"] = audit_record_to_api(record)
    return response_body


@router.post("/facts", status_code=status.HTTP_201_CREATED)
def import_revenue_fact(
    payload: RevenueFactImportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Validate and persist a connector-sourced monthly fact, then audit it."""
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    try:
        source_kind = _validate_connector_source_kind(payload.connector_key, payload.source_kind)
        fact = repository.record_fact(
            month=payload.month,
            youtube_channel_id=payload.youtube_channel_id,
            source_kind=source_kind,
            source_report_id=payload.source_report_id,
            gross_revenue_usd=payload.gross_revenue_usd,
            net_revenue_usd=payload.net_revenue_usd,
            shorts_revenue_usd=payload.shorts_revenue_usd,
            longform_revenue_usd=payload.longform_revenue_usd,
            subscription_revenue_usd=payload.subscription_revenue_usd,
            views=payload.views,
            watch_time_minutes=payload.watch_time_minutes,
            confidence_score=payload.confidence_score,
            actor_user_id=user.user_id,
        )
    except RevenueFactLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REPORT_IMPORTED,
        entity_type="monthly_channel_revenue_fact",
        entity_id=fact.audit_entity_id,
        scope=connector_scope,
        reason=payload.reason,
        details={
            "connector_key": payload.connector_key,
            "source_report_id": payload.source_report_id,
            "gross_revenue_usd": fact.to_api()["gross_revenue_usd"],
            "shorts_revenue_usd": fact.to_api()["shorts_revenue_usd"],
            "longform_revenue_usd": fact.to_api()["longform_revenue_usd"],
            "subscription_revenue_usd": fact.to_api()[
                "subscription_revenue_usd"
            ],
        },
    )
    return _with_audit_event(fact, record)


@router.get("/channels/{channel_id}/months/{month}/facts")
def list_channel_month_revenue_facts(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return all revenue facts recorded for a channel in a given month."""
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts = repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_channel_revenue_fact",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={"fact_count": len(facts)},
    )
    return {
        "month": month,
        "youtube_channel_id": channel_id,
        "facts": [fact.to_api() for fact in facts],
        "audit_event": audit_record_to_api(record),
    }


@router.get("/channels/{channel_id}/months/{month}/reconciliation-preview")
def get_channel_month_reconciliation_preview(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Build and return the multi-source reconciliation preview for a channel and month."""
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    try:
        facts = repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    preview = build_revenue_reconciliation_preview(
        facts,
        month=month,
        youtube_channel_id=channel_id,
    )
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="revenue_reconciliation_preview",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={
            "status": preview.status,
            "issue_count": len(preview.issues),
            "compared_source_count": preview.compared_source_count,
        },
    )
    return preview.to_api()


@router.get("/months/{month}/reconciliation-issues")
def list_month_reconciliation_issues(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """Return the reconciliation issue queue for the caller's channels."""
    # Reject only when the caller has no relevant grant at all; a caller whose
    # scoped grant currently maps to zero channels (e.g. sector/company with
    # no active mapping) should see an empty queue, not 403.
    if user.disabled or not _granted_scopes_for_permission(user, Permission.VIEW_REVENUE):
        _raise_missing_permission(Permission.VIEW_REVENUE)
    if not _granted_scopes_for_permission(user, Permission.VIEW_CONFIDENCE):
        _raise_missing_permission(Permission.VIEW_CONFIDENCE)

    revenue_channel_ids = _authorized_channel_ids_for_permission(
        user,
        Permission.VIEW_REVENUE,
        org_index,
    )
    confidence_channel_ids = _authorized_channel_ids_for_permission(
        user,
        Permission.VIEW_CONFIDENCE,
        org_index,
    )
    visible_channel_ids = _intersect_channel_sets(revenue_channel_ids, confidence_channel_ids)
    if visible_channel_ids is not None and not visible_channel_ids:
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="revenue_reconciliation_issue_queue",
            entity_id=month,
            scope=AccessScope.finance_month(month),
            details={
                "issue_count": 0,
                "page_channel_count": 0,
                "page_fact_count": 0,
                "has_more": False,
                "scoped_channel_count": 0,
            },
        )
        empty_queue = build_revenue_reconciliation_issue_queue([], month=month)
        response = empty_queue.to_api()
        response["pagination"] = {
            "limit": limit,
            "offset": offset,
            "next_offset": None,
            "has_more": False,
        }
        return response

    try:
        page_size = limit + 1
        page_channel_ids = repository.list_month_channel_ids(
            month=month,
            youtube_channel_ids=visible_channel_ids,
            limit=page_size,
            offset=offset,
        )
        channel_ids_for_page = page_channel_ids[:limit]
        facts = repository.list_month_facts(
            month=month,
            youtube_channel_ids=set(channel_ids_for_page),
        )
    except RevenueFactValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    has_more = len(page_channel_ids) > limit
    queue = build_revenue_reconciliation_issue_queue(facts, month=month)
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="revenue_reconciliation_issue_queue",
        entity_id=month,
        scope=AccessScope.finance_month(month),
        details={
            "issue_count": len(queue.items),
            "page_channel_count": len(channel_ids_for_page),
            "page_fact_count": len(facts),
            "has_more": has_more,
            "scoped_channel_count": (
                len(visible_channel_ids) if visible_channel_ids is not None else None
            ),
        },
    )
    response = queue.to_api()
    response["pagination"] = {
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if has_more else None,
        "has_more": has_more,
    }
    return response


@router.get("/months/{month}/payment-match")
def get_month_payment_match(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Compare monthly YouTube revenue facts against AdSense payments."""
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)
    try:
        normalized_currency = normalize_payment_match_currency(currency)
        facts = revenue_repository.list_month_facts(month=month)
        payments = payment_repository.list_month_payments(month=month)
        summary = build_monthly_payment_match_summary(
            month=month,
            facts=facts,
            payments=payments,
            currency=normalized_currency,
        )
    except (
        AdSensePaymentValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary_api = summary.to_api()
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_payment_match",
        entity_id=month,
        scope=revenue_scope,
        details={
            "status": summary.status,
            "youtube_revenue_total_usd": summary_api["youtube_revenue_total_usd"],
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_payment_match",
        entity_id=month,
        scope=payment_scope,
        details={
            "status": summary.status,
            "adsense_paid_amount": summary_api["adsense_paid_amount"],
            "paid_payment_count": summary.paid_payment_count,
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api


@router.get("/months/{month}/smart-alerts")
def get_month_smart_alerts(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    bank_repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    close_repository: Annotated[
        SqlAlchemyFinanceMonthCloseRepository,
        Depends(current_finance_month_close_repository),
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Aggregate cross-domain health signals for a month into a prioritized smart-alert summary."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        facts = revenue_repository.list_month_facts(month=month)
        previous_facts = revenue_repository.list_month_facts(
            month=_previous_month(month)
        )
        payments = payment_repository.list_month_payments(month=month)
        bank_entries = bank_repository.list_month_entries(month=month)
        manual_overrides = override_repository.list_month_overrides(month=month)
        close = close_repository.get(month)
        missing_fact_channel_ids = _missing_revenue_fact_channel_ids(
            session, month=month
        )
        payment_match = build_monthly_payment_match_summary(
            month=month,
            facts=facts,
            payments=payments,
        )
        bank_reconciliation = build_month_bank_reconciliation_summary(
            month=month,
            payments=payments,
            bank_entries=bank_entries,
        )
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
        ManualOverrideValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_monthly_smart_alert_summary(
        month=month,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        close_status=close.status if close else "OPEN",
        manual_overrides=manual_overrides,
        missing_revenue_fact_channel_ids=missing_fact_channel_ids,
        current_revenue_facts=facts,
        previous_revenue_facts=previous_facts,
    )
    summary_api = summary.to_api()
    audit_details = {
        "status": summary.status,
        "alert_count": len(summary.alerts),
        "highest_severity": summary.highest_severity,
    }
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
        audit_record_to_api(bank_record),
    ]
    return summary_api


# ============================================================================
# Purpose: Read-only per-month deduction-evidence view, grouped by scope
#   (CHANNEL/ACCOUNT/PAYMENT). Surfaces the typed components PR-A ingested; never
#   writes, never triggers ingestion, never returns raw_payload.
# Database/ORM: Reads deduction_components via SqlAlchemyDeductionComponentRepository.
# Standards: smart-alerts four-permission auth; audit events match filtered
#   evidence scopes; month validation -> 422; offset/limit pagination.
# Blast Radius: Finance read (deduction evidence). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> repo.
#   - File: backend/ums_smart_revenue/finance/deduction_components.py -> to_api().
# ============================================================================
@router.get(
    "/months/{month}/deduction-components",
    response_model=MonthDeductionComponentsResponse,
)
def get_month_deduction_components(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    component_kind: str | None = None,
    scope_kind: str | None = None,
    scope_id: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MonthDeductionComponentsResponse:
    """Return one month's deduction evidence grouped by scope for finance review."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        page = repository.list_month_components_page(
            month=month,
            component_kind=component_kind,
            scope_kind=scope_kind,
            scope_id=scope_id,
            limit=limit,
            offset=offset,
        )
    except DeductionComponentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    grouped: dict[str, list[dict[str, object]]] = {}
    for component in page.components:
        grouped.setdefault(component.scope_kind, []).append(component.to_api())
    scopes = [
        {
            "scope_kind": total.scope_kind,
            "component_count": total.component_count,
            "total_amount_usd": _decimal_to_api(total.total_amount_usd),
            "components": grouped.get(total.scope_kind, []),
        }
        for total in page.scope_totals
    ]

    audit_details = {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
    }
    scope_kinds = {total.scope_kind for total in page.scope_totals}
    audit_events = []
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    audit_events.append(audit_record_to_api(revenue_record))
    if scope_kinds & {"ACCOUNT", "PAYMENT"}:
        payment_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type="monthly_deduction_components",
            entity_id=month,
            scope=month_scope,
            details=audit_details,
        )
        audit_events.append(audit_record_to_api(payment_record))
    if "PAYMENT" in scope_kinds:
        bank_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
            entity_type="monthly_deduction_components",
            entity_id=month,
            scope=month_scope,
            details=audit_details,
        )
        audit_events.append(audit_record_to_api(bank_record))
    has_more = offset + len(page.components) < page.total_count
    return MonthDeductionComponentsResponse(
        month=month,
        total_count=page.total_count,
        returned_count=len(page.components),
        scopes=scopes,
        pagination={
            "limit": limit,
            "offset": offset,
            "next_offset": (offset + limit) if has_more else None,
            "has_more": has_more,
        },
        audit_events=audit_events,
    )


# ============================================================================
# Purpose: Return the scoped monthly net-revenue summary, including
#   account-allocated net-applicable deductions on the missing-net path only.
# Database/ORM: Reads revenue facts, manual overrides, deduction components,
#   and channel-account links; no writes.
# Standards: Enforce revenue/confidence/payment access before data reads;
#   global-only unallocated-account surface; dual REVENUE_VIEWED/PAYMENT_VIEWED
#   audit events.
# Blast Radius: Finance read path only. No persistence, no graph impact.
# ============================================================================
@router.get("/months/{month}/net-revenue")
def get_month_net_revenue(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    deduction_component_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    scope_type: Annotated[str, Query(min_length=1)] = "global",
    scope_id: str | None = None,
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Return the scoped monthly net-revenue summary for an authorized finance viewer."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
    )
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    _require_permission(
        user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month)
    )
    # FIX: Derive the global-surface gate and audit entity ids from the resolved
    # AccessScope, not the raw scope_type/scope_id query strings. The permission
    # checks above already run on the normalized target_scope, so keying the
    # global-only unallocated surface and the audit ids off the raw strings would
    # diverge (e.g. " global " authorizes as global but would be denied the
    # surface and write a malformed entity id).
    normalized_scope_type = target_scope.type.value
    normalized_scope_id = target_scope.id or "global"
    is_global_scope = target_scope == AccessScope.global_scope()
    try:
        normalized_currency = normalize_net_revenue_currency(currency)
        facts = revenue_repository.list_month_facts(
            month=month,
            youtube_channel_ids=channel_ids,
        )
        overrides = override_repository.list_month_overrides(
            month=month,
            youtube_channel_ids=channel_ids,
        )
        deduction_components = deduction_component_repository.list_month_components(
            month=month,
            youtube_channel_ids=channel_ids,
            component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
        )
        account_result, allocation_provenance = resolve_month_account_allocation(
            month=month,
            session=session,
            deduction_repository=deduction_component_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            committed_repository=committed_repository,
        )
        # FIX: the account allocation resolves month-wide (live compute or the
        # committed snapshot), so a scoped read must drop allocation lines for
        # channels outside the resolved channel_ids before they reach the summary
        # builder; otherwise a caller authorized for one company/sector/channel
        # would receive other channels' allocation-derived rows and totals.
        # channel_ids is None for global reads, which pass through unchanged.
        scoped_account_lines = filter_account_allocations_to_scope(
            account_result.lines, channel_ids
        )
        summary = build_month_net_revenue_summary(
            month=month,
            facts=facts,
            manual_overrides=overrides,
            deduction_components=deduction_components,
            account_allocations=scoped_account_lines,
            unallocated_account_issues=(
                account_result.unallocated if is_global_scope else None
            ),
        )
    except (
        DeductionComponentValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary_api = summary.to_api()
    summary_api["currency"] = normalized_currency
    summary_api.update(allocation_provenance_to_api(allocation_provenance))
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_net_revenue_summary",
        entity_id=f"{month}:{normalized_scope_type}:{normalized_scope_id}",
        scope=target_scope,
        details={
            "status": summary.status,
            "channel_count": summary.channel_count,
            "calculated_channel_count": summary.calculated_channel_count,
            "missing_net_source_count": summary.missing_net_source_count,
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_net_revenue_summary",
        entity_id=f"{month}:{normalized_scope_type}:{normalized_scope_id}",
        scope=AccessScope.finance_month(month),
        details={
            "status": summary.status,
            "channel_count": summary.channel_count,
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api


@router.post(
    "/months/{month}/bank-reconciliation",
    status_code=status.HTTP_201_CREATED,
)
def record_month_bank_reconciliation(
    month: str,
    payload: BankReconciliationRecordRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Persist a bank-received reconciliation entry for a month and emit an audit event."""
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.MANAGE_BANK_RECONCILIATION, scope)
    try:
        entry = repository.record_entry(
            month=month,
            bank_reference=payload.bank_reference,
            bank_received_date=payload.bank_received_date,
            bank_received_amount=payload.bank_received_amount,
            bank_received_currency=payload.bank_received_currency,
            bank_received_amount_usd=payload.bank_received_amount_usd,
            transfer_fee_usd=payload.transfer_fee_usd,
            fx_difference_usd=payload.fx_difference_usd,
            notes=payload.notes,
            source_report_id=payload.source_report_id,
            actor_user_id=user.user_id,
        )
    except BankReconciliationLockedMonthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except BankReconciliationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_RECORDED,
        entity_type="bank_reconciliation_entry",
        entity_id=entry.id,
        scope=AccessScope.finance_month(month),
        reason=payload.reason,
        details={
            "bank_reference": entry.bank_reference,
            "bank_received_amount_usd": entry.to_api()[
                "bank_received_amount_usd"
            ],
        },
    )
    response = entry.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("/months/{month}/bank-reconciliation")
def get_month_bank_reconciliation(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    bank_repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return the bank-reconciliation summary for a month."""
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        payments = payment_repository.list_month_payments(month=month)
        entries = bank_repository.list_month_entries(month=month)
    except (AdSensePaymentValidationError, BankReconciliationValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_month_bank_reconciliation_summary(
        month=month,
        payments=payments,
        bank_entries=entries,
    )
    summary_api = summary.to_api()
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="month_bank_reconciliation",
        entity_id=month,
        scope=AccessScope.finance_month(month),
        details={
            "status": summary.status,
            "entry_count": summary.entry_count,
            "bank_received_amount_usd": summary_api[
                "bank_received_amount_usd"
            ],
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="month_bank_reconciliation",
        entity_id=month,
        scope=scope,
        details={
            "status": summary.status,
            "paid_payment_count": summary.paid_payment_count,
            "adsense_paid_amount_usd": summary_api["adsense_paid_amount_usd"],
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(bank_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api


@router.post("/channels/{channel_id}/months/{month}/explain")
def explain_channel_month_revenue_metric(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    explanation_repository: Annotated[
        SqlAlchemyNumberExplanationRepository,
        Depends(current_number_explanation_repository),
    ],
    deduction_component_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    metric: str = "adjusted_gross_revenue_usd",
) -> dict[str, object]:
    """Generate, persist, and audit a channel-month metric explanation (gross or net)."""
    # ========================================================================
    # Purpose: Generate and persist a channel-month metric explanation. Supports
    #   the gross metric (byte-identical legacy path: VIEW_REVENUE+VIEW_CONFIDENCE
    #   @channel, singular audit_event) and the net_revenue_usd metric (additional
    #   VIEW_FINALIZED_PAYMENTS@finance_month gate, channel-direct + account-
    #   allocated deduction provenance, plural audit_events [REVENUE, PAYMENT]).
    # Database/ORM: NumberExplanationORM (write/upsert); reads RevenueFact,
    #   RevenueManualOverride, DeductionComponent, ChannelAccount link tables.
    # Standards: Thin route; auth fail-closed before any data access; typed
    #   domain errors translated to 403/404/422; logic lives in finance services.
    # Blast Radius: Authorization (net adds a stricter finalized-payment gate;
    #   gross unchanged), audit (net adds PAYMENT_VIEWED), finance explanations.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/explanations.py -> net builder.
    #   - File: backend/ums_smart_revenue/finance/allocation_inputs.py -> month
    #     allocation lines reused for account-allocated net provenance.
    # ========================================================================
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    # FIX: refuse the smart reconciliation metric on the generic explain route.
    # The reconciliation explanation is built and persisted only by the
    # dedicated ReconciliationWorkflowService workflow (see
    # backend/ums_smart_revenue/finance/reconciliation_service.py), which
    # enforces VIEW_FINALIZED_PAYMENTS + VIEW_BANK_RECONCILIATION +
    # VIEW_CONFIDENCE + CHANGE_ALLOCATION_RULE. Routing the metric through
    # this generic endpoint would let a caller with only channel revenue +
    # confidence access overwrite the smart reconciliation row with an
    # adjusted-gross explanation and corrupt the persisted explanation
    # returned by GET /revenue/channels/{id}/months/{month}/reconciliation.
    if metric == REVENUE_RECONCILIATION_METRIC:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{REVENUE_RECONCILIATION_METRIC} is not writable through the "
                "generic explain endpoint; use POST "
                f"/revenue/months/{month}/reconcile instead."
            ),
        )
    if metric not in SUPPORTED_METRICS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Unsupported explanation metric: {metric}. Supported: "
                f"{sorted(SUPPORTED_METRICS)}."
            ),
        )
    is_net_metric = metric == NET_REVENUE_METRIC
    if is_net_metric:
        # Net explanations expose finalized-payment-derived deduction provenance,
        # so gate them at finance_month(month) exactly like the PR-2 net-revenue
        # route (revenue.py:1100-1102). finance_month is not an org-hierarchy
        # scope, so no org_index is passed.
        _require_permission(
            user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month)
        )
    try:
        facts = revenue_repository.list_channel_month_facts(
            month=month,
            youtube_channel_id=channel_id,
        )
        overrides = override_repository.list_channel_month_overrides(
            month=month,
            youtube_channel_id=channel_id,
        )
        deduction_components: list[DeductionComponent] = []
        account_allocations: list[AllocationLine] = []
        account_allocation_provenance = None
        if is_net_metric:
            deduction_components = deduction_component_repository.list_month_components(
                month=month,
                youtube_channel_ids={channel_id},
                component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
            )
            account_result, account_allocation_provenance = resolve_month_account_allocation(
                month=month,
                session=session,
                deduction_repository=deduction_component_repository,
                revenue_repository=revenue_repository,
                link_repository=link_repository,
                committed_repository=committed_repository,
            )
            account_allocations = list(account_result.lines)
        explanation = build_channel_month_revenue_explanation(
            facts=facts,
            manual_overrides=overrides,
            month=month,
            youtube_channel_id=channel_id,
            metric=metric,
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            account_allocation_provenance=account_allocation_provenance,
        )
        explanation_repository.record_explanation(explanation)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        DeductionComponentValidationError,
        ManualOverrideValidationError,
        NumberExplanationValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if is_net_metric:
        revenue_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="number_explanation",
            entity_id=f"{channel_id}:{month}:{metric}",
            scope=target_scope,
            details={"metric": metric, "warning_count": len(explanation.warnings)},
        )
        payment_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type="finance_month",
            entity_id=month,
            scope=AccessScope.finance_month(month),
            details={"metric": metric},
        )
        response = explanation.to_api()
        response["audit_events"] = [
            audit_record_to_api(revenue_record),
            audit_record_to_api(payment_record),
        ]
        return response

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="number_explanation",
        entity_id=f"{channel_id}:{month}:{metric}",
        scope=target_scope,
        details={"metric": metric, "warning_count": len(explanation.warnings)},
    )
    response = explanation.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/manual-overrides", status_code=status.HTTP_201_CREATED)
def create_manual_override(
    payload: ManualOverrideCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Create a pending manual revenue adjustment for a channel-month and emit an audit event."""
    target_scope = AccessScope.channel(payload.youtube_channel_id)
    _require_permission(user, Permission.CREATE_MANUAL_OVERRIDE, target_scope, org_index)
    try:
        override = repository.create_override(
            month=payload.month,
            youtube_channel_id=payload.youtube_channel_id,
            adjustment_revenue_usd=payload.adjustment_revenue_usd,
            reason=payload.reason,
            actor_user_id=user.user_id,
        )
    except ManualOverrideLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MANUAL_OVERRIDE_CREATED,
        entity_type="revenue_manual_override",
        entity_id=override.id,
        scope=target_scope,
        reason=payload.reason,
        details={
            "month": override.month,
            "youtube_channel_id": override.youtube_channel_id,
            "adjustment_revenue_usd": override.to_api()["adjustment_revenue_usd"],
        },
    )
    return _manual_override_with_audit_event(override, record)


@router.post("/manual-overrides/{manual_override_id}/approve")
def approve_manual_override(
    manual_override_id: str,
    payload: ManualOverrideApprovalRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Approve a pending manual revenue override after scoped auth."""
    if (
        _authorized_channel_ids_for_permission(
            user,
            Permission.APPROVE_MANUAL_OVERRIDE,
            org_index,
        )
        == set()
    ):
        _raise_missing_permission(Permission.APPROVE_MANUAL_OVERRIDE)

    try:
        target_channel_id = repository.get_override_channel_id(manual_override_id)
    except ManualOverrideValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if target_channel_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manual override not found",
        )

    target_scope = AccessScope.channel(target_channel_id)
    if not has_permission(user, Permission.APPROVE_MANUAL_OVERRIDE, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manual override not found",
        )
    try:
        override = repository.approve_override(
            override_id=manual_override_id,
            actor_user_id=user.user_id,
            reason=payload.reason,
        )
    except ManualOverrideNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ManualOverrideConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MANUAL_OVERRIDE_APPROVED,
        entity_type="revenue_manual_override",
        entity_id=override.id,
        scope=target_scope,
        reason=payload.reason,
        details={
            "month": override.month,
            "youtube_channel_id": override.youtube_channel_id,
            "adjustment_revenue_usd": override.to_api()["adjustment_revenue_usd"],
        },
    )
    return _manual_override_with_audit_event(override, record)


@router.get("/channels/{channel_id}/months/{month}/summary")
def get_channel_month_revenue_summary(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return the adjusted-revenue summary for a channel and month, including manual overrides."""
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts = revenue_repository.list_channel_month_facts(
            month=month,
            youtube_channel_id=channel_id,
        )
        overrides = override_repository.list_channel_month_overrides(
            month=month,
            youtube_channel_id=channel_id,
        )
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ManualOverrideValidationError, RevenueFactValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_adjusted_revenue_summary(
        facts=facts,
        manual_overrides=overrides,
        month=month,
        youtube_channel_id=channel_id,
    )
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="adjusted_revenue_summary",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={
            "status": summary.status,
            "approved_manual_override_count": summary.approved_manual_override_count,
            "pending_manual_override_count": summary.pending_manual_override_count,
        },
    )
    return summary.to_api()


def _require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex | None = None,
) -> None:
    """Raise HTTP 403 if the principal does not hold the given permission for the given scope."""
    if not has_permission(user, permission, scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _raise_missing_permission(permission: Permission) -> None:
    """Unconditionally raise HTTP 403 for a missing permission without revealing caller details."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _authorized_channel_ids_for_permission(
    user: UserPrincipal,
    permission: Permission,
    org_index: OrgAccessIndex,
) -> set[str] | None:
    """Resolve permitted channel IDs for a permission, or None for global."""
    if user.disabled:
        return set()

    channel_ids: set[str] = set()
    for scope in _granted_scopes_for_permission(user, permission):
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
    user: UserPrincipal,
    permission: Permission,
) -> tuple[AccessScope, ...]:
    """Collect all active direct or role scopes granting a permission."""
    scopes: list[AccessScope] = []
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            scopes.append(grant.scope)
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset()):
            scopes.append(assignment.scope)
    return tuple(scopes)


def _intersect_channel_sets(left: set[str] | None, right: set[str] | None) -> set[str] | None:
    """Intersect two nullable channel-ID sets, treating None as the universe (no restriction)."""
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _previous_month(month: str) -> str:
    """Return the YYYY-MM string for the calendar month immediately preceding the given month."""
    if not MONTH_VALUE_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    try:
        year_value, month_value = month.split("-", maxsplit=1)
        year = int(year_value)
        month_number = int(month_value)
    except ValueError as exc:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        ) from exc
    if year == 0 or month_number < 1 or month_number > 12:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    if month_number == 1:
        if year == 1:
            raise RevenueFactValidationError(
                "month must use YYYY-MM with a calendar month from 01 to 12"
            )
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"


# ============================================================================
# Purpose: Read the active, revenue-required channels that have no revenue fact
#   for the month, so the smart-alert builder can emit per-channel coverage gaps.
# Database/ORM: Read-only LEFT JOIN of YouTubeChannelORM x
#   MonthlyChannelRevenueFactORM (no FOR UPDATE), tenant-scoped, source of truth
#   in PostgreSQL.
# Standards: Mirrors month_close_readiness._missing_required_revenue_fact_count
#   exactly (active.is_(True) AND revenue_required.is_(True) AND fact.id IS NULL);
#   returns ids (not a count). No write, no lock, no Neo4j.
# Blast Radius: Finance read surface only; no auth/audit/finance mutation.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
#     shared query shape (count there; ids here).
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes ids.
# ============================================================================
def _missing_revenue_fact_channel_ids(session: Session, *, month: str) -> list[str]:
    """Return active revenue-required channel ids with no fact for the month."""
    tenant_id = _resolve_smart_alert_tenant_id()
    statement = (
        select(YouTubeChannelORM.youtube_channel_id)
        .select_from(YouTubeChannelORM)
        .outerjoin(
            MonthlyChannelRevenueFactORM,
            (MonthlyChannelRevenueFactORM.tenant_id == YouTubeChannelORM.tenant_id)
            & (
                MonthlyChannelRevenueFactORM.youtube_channel_id
                == YouTubeChannelORM.youtube_channel_id
            )
            & (MonthlyChannelRevenueFactORM.tenant_id == tenant_id)
            & (MonthlyChannelRevenueFactORM.month == month),
        )
        .where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.revenue_required.is_(True),
            MonthlyChannelRevenueFactORM.id.is_(None),
        )
    )
    return list(session.scalars(statement).all())


def _resolve_smart_alert_tenant_id() -> UUID:
    """Resolve the request tenant id, mirroring the finance repositories."""
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return UUID(UMS_TENANT_ID)


def _revenue_read_scope_to_channel_ids(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
) -> tuple[AccessScope, set[str] | None]:
    """Translate a revenue read scope into an AccessScope and channel filter."""
    normalized_scope_type = scope_type.strip()
    normalized_scope_id = (
        scope_id.strip() if isinstance(scope_id, str) else scope_id
    )
    if normalized_scope_type == "global":
        if normalized_scope_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="scope_id must be omitted for global revenue reads",
            )
        return AccessScope.global_scope(), None
    if not normalized_scope_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "scope_id is required for revenue scope_type: "
                f"{normalized_scope_type}"
            ),
        )
    if normalized_scope_type == "sector":
        return (
            AccessScope.sector(normalized_scope_id),
            {
                channel_id
                for channel_id, sector_id in org_index.channel_sector.items()
                if sector_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "company":
        return (
            AccessScope.company(normalized_scope_id),
            {
                channel_id
                for channel_id, company_id in org_index.channel_company.items()
                if company_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "channel":
        return AccessScope.channel(normalized_scope_id), {normalized_scope_id}
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"Unknown revenue scope_type: {scope_type}",
    )


def audit_record_to_api(record: AuditRecord) -> dict[str, object]:
    """Serialize an AuditRecord to the safe API-facing dictionary shape."""
    return {
        "event_type": record.event_type,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "reason": record.reason,
        "sensitive": record.sensitive,
    }


def _with_audit_event(fact: RevenueFactEntry, record: AuditRecord) -> dict[str, object]:
    """Attach the serialized audit event to a revenue-fact API response dictionary."""
    response = fact.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _manual_override_with_audit_event(
    override: RevenueManualOverrideEntry,
    record: AuditRecord,
) -> dict[str, object]:
    """Attach the serialized audit event to a manual-override API response dictionary."""
    response = override.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _strip_required_string(value):
    """Strip whitespace from a string value, raising ValueError if the result is empty."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


def _validate_connector_source_kind(connector_key: str, source_kind: str) -> str:
    """Validate and return the normalized source_kind value allowed for the given connector_key."""
    try:
        normalized_source_kind = RevenueFactSourceKind(source_kind).value
    except ValueError as exc:
        raise RevenueFactValidationError(
            f"Unknown revenue fact source_kind: {source_kind}"
        ) from exc

    allowed_source_kinds = _REVENUE_SOURCE_KINDS_BY_CONNECTOR_KEY.get(connector_key)
    if allowed_source_kinds is None:
        raise RevenueFactValidationError(f"Unknown revenue fact connector_key: {connector_key}")
    if normalized_source_kind not in allowed_source_kinds:
        raise RevenueFactValidationError(
            f"connector_key {connector_key} cannot import source_kind {normalized_source_kind}"
        )
    return normalized_source_kind
