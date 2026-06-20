# ============================================================================
# Purpose: Serve the finance revenue read API (facts, allocation, smart-alerts,
#   monthly summaries, reconciliation, explanation). Routes are thin: they
#   parse input, enforce typed permissions + tenant/scope boundaries, call
#   finance + repository services, and translate typed domain errors into
#   HTTP responses. No finance math or audit writes happen here.
# Database/ORM: Reads/writes via SQLAlchemy repositories
#   (RevenueFactRepository, AdSensePaymentRepository, BankReconciliationRepository,
#   ManualOverrideRepository, FinanceMonthCloseRepository, AllocationRepository)
#   plus tenant-scoped AuditLogORM reads for smart-alert inputs.
# Standards: smart-alerts four-permission gate (VIEW_REVENUE/VIEW_CONFIDENCE
#   global + VIEW_FINALIZED_PAYMENTS/VIEW_BANK_RECONCILIATION month-scoped);
#   audit-derived inputs gated by VIEW_AUDIT_LOG with VIEW_SENSITIVE_AUDIT_PAYLOADS
#   controlling reason redaction; tenant_id resolved once per request; month
#   validated to YYYY-MM; typed errors translated to 4xx via HTTPException.
# Blast Radius: Finance read surface, audit observability surface, and the
#   smart-alerts authorization boundary. No finance writes, no Neo4j, no
#   matching/close behavior change.
# Connections:
#   - File: backend/ums_smart_revenue/finance/* -> pure builders (smart_alerts,
#     payment_matching, bank_reconciliation, net_revenue, manual_overrides).
#   - File: backend/ums_smart_revenue/api/exports.py -> mirrors the smart-alerts
#     read path so exported workbooks surface the same alerts.
#   - File: backend/ums_smart_revenue/auth/audit.py -> audit-log gate pattern
#     that this module follows for the audit-derived skipped-row signal.
#   - File: Docs/12_BACKEND_API_SPEC.md -> endpoint contracts and alert codes.
# ============================================================================
import re
from datetime import date
from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Integer, literal_column, select
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
from ums_smart_revenue.api.org_units import current_org_unit_reader
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
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.connectors.runs.audit_alerts import (
    failed_connector_run_count_and_statuses,
)
from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM
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
from ums_smart_revenue.finance.rankings import (
    RankingsValidationError,
    build_month_rankings,
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
from ums_smart_revenue.finance.revenue_scopes import build_authorized_revenue_scopes
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary
from ums_smart_revenue.finance.smart_alerts import (
    MonthlySmartAlertAuditSignals,
    MonthlySmartAlertFinanceInputs,
    MonthlySmartAlertTrendSignals,
    build_monthly_smart_alert_summary,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.org.org_units_read import SqlAlchemyOrgUnitReader
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
#   explicit-lines commit endpoint). Returns the NORMALIZED allocation method and
#   non-empty idempotency key for the downstream commit so validation and the
#   service agree on casing and non-null write identity.
# Database/ORM: None.
# Standards: typed 422s with safe, actionable messages; runs AFTER the auth gates
#   (authorization-before-validation parity with the commit endpoint).
# Blast Radius: Authorization order + write-path validation. No finance number.
# ============================================================================
def _validate_recalculation_write_request(
    payload: RevenueRecalculationRequest,
) -> tuple[str, str]:
    """Validate the dry_run=False request shape; return the normalized write context."""
    if payload.scope_type.strip() != "global":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="committed recalculation requires scope_type=global",
        )
    idempotency_key = payload.idempotency_key
    if not idempotency_key:
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
    return normalized_method, idempotency_key


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
    idempotency_key: str,
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
        allocation_method=normalized_method,
        reason=payload.reason,
    )
    try:
        outcome = committed_repository.commit_allocation(
            month=payload.month,
            allocation_method=normalized_method,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            reason=payload.reason,
            committed_by=user.user_id,  # str; repo -> UUID
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
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

    write_status = "COMMITTED" if outcome.created else "IDEMPOTENT_REPLAY"
    commit_audit_event = emit_allocation_committed_audit(
        sink=audit_sink,
        actor=user,
        month=payload.month,
        scope=month_scope,
        reason=payload.reason,
        outcome=outcome,
    )
    response.status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
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
    group_registry: Annotated[
        ChannelGroupRegistryStore,
        Depends(sql_group_registry_from_session),
    ],
    response: Response,
) -> dict[str, object]:
    """Preview (dry_run) or commit (dry_run=false) a revenue recalculation."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        org_index=org_index,
        group_registry=group_registry,
    )
    month_scope = AccessScope.finance_month(payload.month)
    # Gates first (authorization before request-shape validation). The write path
    # must never be weaker than the commit endpoint: dry_run=false adds the
    # write-only VIEW_FINALIZED_PAYMENTS gate right after the two dry-run gates.
    _require_revenue_read_permission(
        user,
        Permission.VIEW_REVENUE,
        target_scope,
        channel_ids,
        org_index,
    )
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, month_scope)
    if not payload.dry_run:
        _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)

    normalized_write_method: str | None = None
    write_idempotency_key: str | None = None
    if not payload.dry_run:
        normalized_write_method, write_idempotency_key = _validate_recalculation_write_request(
            payload
        )

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
            verified_channel_ids=(frozenset(channel_ids) if channel_ids is not None else None),
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
        if normalized_write_method is None or write_idempotency_key is None:
            raise RuntimeError("validated recalculation write context missing")
        # FIX: check idempotency BEFORE the preflight gate. If a run was already
        # committed under this key the preflight is irrelevant — the request is a
        # replay regardless of what the current facts look like. Without this check
        # a valid committed run would return 409 BLOCKED_BY_ISSUES on retry once
        # facts change (e.g. a net-revenue row is removed), which is wrong because
        # the key/fingerprint already identifies a completed write.
        _existing_run = committed_repository.get_run_by_idempotency_key(
            month=payload.month,
            idempotency_key=write_idempotency_key,
        )
        if _existing_run is None:
            # No existing run — enforce the preflight gate before any write.
            if preview.blocking_issues:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "write_status": "BLOCKED_BY_ISSUES",
                        "blocking_issues": [issue.to_api() for issue in preview.blocking_issues],
                    },
                )
        write_fragment = _commit_recalculation_write(
            payload=payload,
            normalized_method=normalized_write_method,  # set on the write path
            idempotency_key=write_idempotency_key,
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

    final_write_status = write_fragment["write_status"] if write_fragment else preview.write_status
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
            "subscription_revenue_usd": fact.to_api()["subscription_revenue_usd"],
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


# ============================================================================
# Purpose: Serve the monthly smart-alerts dashboard endpoint. Aggregates
#   cross-domain finance health signals (payment match, bank reconciliation,
#   coverage gap, audit-derived skipped source rows, audit-derived failed
#   connector runs, overrides, MoM revenue anomaly, close status) into a
#   prioritized alert summary + self-audit trail. Read-only; never mutates
#   finance numbers.
# Database/ORM: Reads via RevenueFact/AdSensePayment/BankReconciliation/
#   ManualOverride/FinanceMonthClose repositories plus a tenant-scoped
#   AuditLogORM scans for ROWS_SKIPPED and FINISHED connector-run edges.
# Standards: smart-alerts four-permission gate (VIEW_REVENUE/VIEW_CONFIDENCE
#   global + VIEW_FINALIZED_PAYMENTS/VIEW_BANK_RECONCILIATION month-scoped).
#   Audit-derived inputs require VIEW_AUDIT_LOG; without it the alert is
#   omitted (return 0,{}) so finance viewers do not bypass the audit gate.
#   Per-reason breakdown requires VIEW_SENSITIVE_AUDIT_PAYLOADS; without it
#   the count is returned but the breakdown is redacted, mirroring audit.py.
# Blast Radius: Finance dashboard + audit-observability boundary. The audit
#   gate is the security-relevant change; do not weaken it without an owner
#   review. No money/ingestion/match/close behavior change.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> pure builder
#     that consumes the (count, reasons) aggregate.
#   - File: backend/ums_smart_revenue/auth/audit.py -> same VIEW_AUDIT_LOG
#     pattern with redaction via VIEW_SENSITIVE_AUDIT_PAYLOADS.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits the ROWS_SKIPPED audit edges this endpoint aggregates.
#   - File: Docs/12_BACKEND_API_SPEC.md -> alert code wire contract.
# ============================================================================
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
    # FIX: Audit-derived inputs require VIEW_AUDIT_LOG; without it the
    # SOURCE_ROWS_SKIPPED alert is omitted entirely so finance viewers do
    # not silently gain access to audit payloads. Per-reason breakdown
    # additionally requires VIEW_SENSITIVE_AUDIT_PAYLOADS; without it the
    # count is returned but the breakdown is redacted, mirroring audit.py.
    audit_scope = AccessScope.global_scope()
    can_view_audit_log = has_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    include_sensitive_details = can_view_audit_log and has_permission(
        user, Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS, audit_scope
    )
    try:
        facts = revenue_repository.list_month_facts(month=month)
        previous_facts = revenue_repository.list_month_facts(month=_previous_month(month))
        payments = payment_repository.list_month_payments(month=month)
        bank_entries = bank_repository.list_month_entries(month=month)
        manual_overrides = override_repository.list_month_overrides(month=month)
        close = close_repository.get(month)
        audit_signals = _month_smart_alert_audit_signals(
            session,
            month=month,
            can_view_audit_log=can_view_audit_log,
            include_sensitive_details=include_sensitive_details,
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
        finance=MonthlySmartAlertFinanceInputs(
            payment_match=payment_match,
            bank_reconciliation=bank_reconciliation,
            close_status=close.status if close else "OPEN",
            manual_overrides=manual_overrides,
        ),
        audit_signals=audit_signals,
        trend_signals=MonthlySmartAlertTrendSignals(
            current_revenue_facts=facts,
            previous_revenue_facts=previous_facts,
        ),
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
    # FIX: Record an AUDIT_LOG_VIEWED self-audit on every audit-derived read
    # of the CONNECTOR_JOB_RUN audit-log scan for this finance month, matching
    # the /audit/events redaction-on-use pattern. The audit trail is emitted
    # when the caller has VIEW_AUDIT_LOG regardless of whether the helper
    # returned data; the `returned` field carries 0 or 1 so a clean month
    # still leaves a record of the read. `details_redacted` reflects whether
    # sensitive per-reason skipped-row breakdown was redacted in the alert
    # response, mirroring /audit/events: True only when that alert actually
    # surfaced data AND the caller lacks VIEW_SENSITIVE_AUDIT_PAYLOADS.
    audit_records = [revenue_record, payment_record, bank_record]
    if can_view_audit_log:
        audit_records.append(
            _record_month_connector_smart_alert_audit(
                audit_sink=audit_sink,
                user=user,
                month=month,
                audit_scope=audit_scope,
                audit_signals=audit_signals,
                include_sensitive_details=include_sensitive_details,
            )
        )
    summary_api["audit_events"] = [audit_record_to_api(r) for r in audit_records]
    return summary_api


# ============================================================================
# Purpose: Emit the AUDIT_LOG_VIEWED self-audit for connector-backed monthly
#   smart-alert reads without adding branch-heavy bookkeeping to the route.
# Database/ORM: AuditSink append only; no direct SQLAlchemy reads.
# Standards: Caller has already passed VIEW_AUDIT_LOG; details preserve the
#   same redaction and returned flags as get_month_smart_alerts.
# Blast Radius: Finance dashboard audit trail only.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> provides the
#     connector-backed smart-alert aggregate counts used in the audit details.
# ============================================================================
def _record_month_connector_smart_alert_audit(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    month: str,
    audit_scope: AccessScope,
    audit_signals: MonthlySmartAlertAuditSignals,
    include_sensitive_details: bool,
) -> AuditRecord:
    """Record that a monthly smart-alert response read connector audit signals."""
    return record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.AUDIT_LOG_VIEWED,
        entity_type="audit_log_page",
        entity_id=f"{month}:connector_smart_alerts",
        scope=audit_scope,
        details=_month_connector_smart_alert_audit_details(
            month=month,
            audit_signals=audit_signals,
            include_sensitive_details=include_sensitive_details,
        ),
    )


def _month_connector_smart_alert_audit_details(
    *,
    month: str,
    audit_signals: MonthlySmartAlertAuditSignals,
    include_sensitive_details: bool,
) -> dict[str, object]:
    """Build stable audit details for connector-backed smart-alert reads."""
    source_rows_skipped_returned = audit_signals.skipped_source_row_count > 0
    connector_runs_failed_returned = audit_signals.failed_connector_run_count > 0
    return {
        "event_type": AuditEventType.CONNECTOR_JOB_RUN.value,
        "entity_type": "monthly_smart_alerts",
        "entity_id": month,
        "returned": int(source_rows_skipped_returned or connector_runs_failed_returned),
        "source_rows_skipped_returned": int(source_rows_skipped_returned),
        "connector_runs_failed_returned": int(connector_runs_failed_returned),
        "details_redacted": source_rows_skipped_returned and not include_sensitive_details,
    }


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

    grouped: dict[str, list[DeductionComponentApiItem]] = {}
    for component in page.components:
        grouped.setdefault(component.scope_kind, []).append(
            DeductionComponentApiItem.model_validate(component.to_api())
        )
    scopes = [
        DeductionComponentScopeGroup(
            scope_kind=total.scope_kind,
            component_count=total.component_count,
            total_amount_usd=_decimal_to_api(total.total_amount_usd),
            components=grouped.get(total.scope_kind, []),
        )
        for total in page.scope_totals
    ]

    audit_details = {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
    }
    scope_kinds = {total.scope_kind for total in page.scope_totals}
    audit_events: list[AuditEventResponse] = []
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    audit_events.append(audit_record_to_response(revenue_record))
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
        audit_events.append(audit_record_to_response(payment_record))
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
        audit_events.append(audit_record_to_response(bank_record))
    has_more = offset + len(page.components) < page.total_count
    return MonthDeductionComponentsResponse(
        month=month,
        total_count=page.total_count,
        returned_count=len(page.components),
        scopes=scopes,
        pagination=DeductionComponentsPagination(
            limit=limit,
            offset=offset,
            next_offset=(offset + limit) if has_more else None,
            has_more=has_more,
        ),
        audit_events=audit_events,
    )


# ============================================================================
# Purpose: Return ONLY the rollup scopes the caller is VIEW_REVENUE-authorized
#   to aggregate (global / their sectors / their companies / channel groups),
#   so the Command Center scope selector cannot offer an out-of-scope unit
#   (org-structure leak) or a dead option that would 403 on the rollup read.
# Database/ORM: OrgUnitORM names via SqlAlchemyOrgUnitReader; the org-access
#   index (company_sector) via current_org_access_index; channel-group rows via
#   ChannelGroupRegistryStore. Read-only, no writes.
# Standards: Thin route — fail-closed VIEW_REVENUE gate at the boundary, then a
#   pure service build, then typed serialization. No audit event (metadata
#   helper like GET /org-units; it discloses only ids/names the caller is
#   authorized for, never a revenue number, so no REVENUE_VIEWED is emitted).
# Blast Radius: Authorization — the anti-scope-leak surface for the selector.
#   A disabled principal or one with no active VIEW_REVENUE grant in any scope
#   gets 403, never a silent empty list. No finance totals, audit, Neo4j, or
#   export impact.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_scopes.py ->
#       build_authorized_revenue_scopes (the expansion/dedup/order logic).
#   - File: backend/ums_smart_revenue/api/org_units.py -> current_org_unit_reader
#       (shared name reader) and the GET /org-units no-audit precedent.
# ============================================================================
@router.get("/scopes")
def list_authorized_revenue_scopes(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    reader: Annotated[SqlAlchemyOrgUnitReader, Depends(current_org_unit_reader)],
    group_registry: Annotated[
        ChannelGroupRegistryStore,
        Depends(sql_group_registry_from_session),
    ],
) -> dict[str, object]:
    """Return the caller's authorized rollup scope options; 403 without VIEW_REVENUE."""
    granted = _granted_scopes_for_permission(user, Permission.VIEW_REVENUE)
    if user.disabled or not granted:
        _raise_missing_permission(Permission.VIEW_REVENUE)

    sector_names: dict[str, str] = {}
    company_names: dict[str, str] = {}
    for unit in reader.list_active_units():
        if unit.type == "SECTOR":
            sector_names[unit.id] = unit.name
        elif unit.type == "COMPANY":
            company_names[unit.id] = unit.name

    options = build_authorized_revenue_scopes(
        granted=granted,
        org_index=org_index,
        sector_names=sector_names,
        company_names=company_names,
        groups=tuple(group_registry.list_groups()),
    )
    if not options:
        # FIX (review #102): Fail-closed when a viewer's VIEW_REVENUE grants
        # produce NO rollup options (e.g. only channel/connector/finance-month
        # scope types, which the builder deliberately drops). Returning an empty
        # list with HTTP 200 would let the frontend treat the success as a signal
        # to fall back to a synthetic GLOBAL option and fire an unauthorized
        # global read. 403 keeps this rollup-scope listing authoritative and
        # matches the no-rollup-scope -> no-permission contract.
        _raise_missing_permission(Permission.VIEW_REVENUE)
    return {"scopes": [option.to_api() for option in options]}


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
    group_registry: Annotated[
        ChannelGroupRegistryStore,
        Depends(sql_group_registry_from_session),
    ],
    scope_type: Annotated[str, Query(min_length=1)] = "global",
    scope_id: str | None = None,
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Return the scoped monthly net-revenue summary for an authorized finance viewer."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
    )
    _require_revenue_read_permission(
        user,
        Permission.VIEW_REVENUE,
        target_scope,
        channel_ids,
        org_index,
    )
    _require_revenue_read_permission(
        user,
        Permission.VIEW_CONFIDENCE,
        target_scope,
        channel_ids,
        org_index,
    )
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
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
            unallocated_account_issues=(account_result.unallocated if is_global_scope else None),
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


# ============================================================================
# Purpose: Return finance-gated, scope-safe company/sector/channel/group
#   rankings for a month, rolled up from the per-channel net-revenue summary.
# Database/ORM: Reads revenue facts, manual overrides, deduction components,
#   channel-account links (committed snapshot for LOCKED months), and org-unit
#   names; no writes.
# Standards: Thin route — enforce VIEW_REVENUE@target + VIEW_CONFIDENCE@target +
#   VIEW_FINALIZED_PAYMENTS@finance_month BEFORE any read; restrict the channel
#   set to the authorized scope BEFORE ranking; logic lives in build_month_rankings
#   and build_month_net_revenue_summary; dual REVENUE_VIEWED/PAYMENT_VIEWED audit;
#   typed errors -> HTTP 422 at the boundary only.
# Blast Radius: Finance read path only. No persistence, no auth weakening, no
#   graph impact. A scoped read with no in-scope channels returns empty rankings
#   (NOT 403); cross-scope channels/companies/sectors never leak into the result.
# Connections:
#   - File: backend/ums_smart_revenue/finance/rankings.py -> build_month_rankings.
#   - File: backend/ums_smart_revenue/finance/net_revenue.py ->
#       build_month_net_revenue_summary / filter_account_allocations_to_scope.
#   - File: backend/ums_smart_revenue/finance/account_allocation_read.py ->
#       resolve_month_account_allocation (committed snapshot for LOCKED months).
# ============================================================================
@router.get("/months/{month}/rankings")
def get_month_rankings(
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
    group_registry: Annotated[
        ChannelGroupRegistryStore,
        Depends(sql_group_registry_from_session),
    ],
    scope_type: Annotated[str, Query(min_length=1)] = "global",
    scope_id: str | None = None,
    metric: Annotated[str, Query(min_length=1)] = "gross",
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict[str, object]:
    """Return scoped month rankings for an authorized finance viewer."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
    )
    _require_revenue_read_permission(
        user,
        Permission.VIEW_REVENUE,
        target_scope,
        channel_ids,
        org_index,
    )
    _require_revenue_read_permission(
        user,
        Permission.VIEW_CONFIDENCE,
        target_scope,
        channel_ids,
        org_index,
    )
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
    normalized_scope_type = target_scope.type.value
    normalized_scope_id = target_scope.id or "global"
    try:
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
        # Scope-leak guard: account allocation resolves month-wide, so drop lines
        # for channels outside the authorized channel_ids before they reach the
        # summary builder. channel_ids is None for global reads (no restriction).
        scoped_account_lines = filter_account_allocations_to_scope(
            account_result.lines, channel_ids
        )
        summary = build_month_net_revenue_summary(
            month=month,
            facts=facts,
            manual_overrides=overrides,
            deduction_components=deduction_components,
            account_allocations=scoped_account_lines,
            # Rankings never surface the global unallocated-account diagnostic: it
            # is a month-wide-only list, not a ranked dimension, so it is
            # intentionally None on every scope (global and scoped) here. Omitting
            # it is strictly safer than leaking a month-wide diagnostic into a
            # scoped ranking response.
            unallocated_account_issues=None,
        )
        company_names, sector_names = _org_unit_name_maps(session)
        channel_names = _channel_name_map(session)
        rankings = build_month_rankings(
            summary=summary,
            channel_company=org_index.channel_company,
            channel_sector=org_index.channel_sector,
            company_names=company_names,
            sector_names=sector_names,
            channel_names=channel_names,
            metric=metric,
            limit=limit,
        )
    except (
        DeductionComponentValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        RankingsValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    response = rankings.to_api()
    response.update(allocation_provenance_to_api(allocation_provenance))
    entity_id = f"{month}:{normalized_scope_type}:{normalized_scope_id}"
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_rankings",
        entity_id=entity_id,
        scope=target_scope,
        details={
            "metric": rankings.metric,
            "channel_count": len(rankings.channels),
            "company_count": len(rankings.companies),
            "sector_count": len(rankings.sectors),
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_rankings",
        entity_id=entity_id,
        scope=AccessScope.finance_month(month),
        details={"metric": rankings.metric},
    )
    response["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    return response


def _org_unit_name_maps(
    session: Session,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (company_id->name, sector_id->name) maps for the current tenant."""
    company_names: dict[str, str] = {}
    sector_names: dict[str, str] = {}
    for unit in SqlAlchemyOrgUnitReader(session).list_active_units():
        if unit.type == "COMPANY":
            company_names[unit.id] = unit.name
        elif unit.type == "SECTOR":
            sector_names[unit.id] = unit.name
    return company_names, sector_names


# ============================================================================
# Purpose: Return {youtube_channel_id -> channel_name} for the current tenant's
#   ACTIVE channels so channel rankings show real names (raw-id fallback applied
#   by build_month_rankings when a channel is missing from this map).
# Database/ORM: Read-only SELECT on YouTubeChannelORM, tenant-scoped, no lock.
# Standards: Tenant resolved exactly like the smart-alert reader
#   (_resolve_smart_alert_tenant_id -> get_current_tenant, UMS_TENANT_ID
#   fallback); never hardcoded. Read surface only — no write/auth/audit.
# Blast Radius: Finance read display only; names never feed totals or ordering.
# Connections:
#   - File: backend/ums_smart_revenue/finance/rankings.py -> build_month_rankings
#       consumes channel_names for RankedEntry.entity_name on channel rows.
# ============================================================================
def _channel_name_map(session: Session) -> dict[str, str]:
    """Return active channel id->name for the current tenant (raw-id fallback)."""
    tenant_id = _resolve_smart_alert_tenant_id()
    statement = select(
        YouTubeChannelORM.youtube_channel_id,
        YouTubeChannelORM.channel_name,
    ).where(
        YouTubeChannelORM.tenant_id == tenant_id,
        YouTubeChannelORM.active.is_(True),
    )
    rows = cast("list[tuple[str, str]]", session.execute(statement).all())
    return dict(rows)


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
            "bank_received_amount_usd": entry.to_api()["bank_received_amount_usd"],
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
            "bank_received_amount_usd": summary_api["bank_received_amount_usd"],
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
                f"Unsupported explanation metric: {metric}. Supported: {sorted(SUPPORTED_METRICS)}."
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


# ============================================================================
# Purpose: Enforce revenue-read permissions for direct org scopes and channel
#   groups. Groups are not stored as grant scopes; they are authorized by
#   requiring the requested permission on every member channel.
# Database/ORM: None; group membership was resolved before this helper.
# Standards: Fail-closed authorization helper; no data reads, no side effects,
#   no widened role/grant storage. For groups, the check is a single subset
#   test over a precomputed covered-channel set instead of one
#   has_permission call per member channel.
# Blast Radius: Authorization boundary for revenue/confidence reads.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> OrgAccessIndex.contains
#       for channel containment under global/sector/company/channel grants.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> group channel
#       membership source resolved by _revenue_read_scope_to_channel_ids.
# ============================================================================
def _require_revenue_read_permission(
    user: UserPrincipal,
    permission: Permission,
    target_scope: AccessScope,
    channel_ids: set[str] | None,
    org_index: OrgAccessIndex,
) -> None:
    """Raise HTTP 403 unless the principal can read the resolved revenue scope."""
    if target_scope.type != ScopeType.GROUP:
        _require_permission(user, permission, target_scope, org_index)
        return
    if not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="group revenue reads require at least one channel",
        )
    # FIX (Qodo review #122 #performance + #security): Reuse the existing
    # disabled-aware _authorized_channel_ids_for_permission helper so the
    # group branch honors the same defense-in-depth user.disabled guard as
    # the per-channel has_permission path, and the covered set is computed
    # once per request instead of once per member channel.
    covered = _authorized_channel_ids_for_permission(user, permission, org_index)
    if covered is not None and not set(channel_ids).issubset(covered):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


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


def _direct_scopes_for_permission(user: UserPrincipal, permission: Permission) -> list[AccessScope]:
    """Yield active direct scopes granting the permission."""
    return [
        grant.scope
        for grant in user.direct_permissions
        if grant.active and grant.permission == permission
    ]


def _role_scopes_for_permission(user: UserPrincipal, permission: Permission) -> list[AccessScope]:
    """Yield active role-derived scopes granting the permission."""
    return [
        assignment.scope
        for assignment in user.role_assignments
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset())
    ]


def _granted_scopes_for_permission(
    user: UserPrincipal,
    permission: Permission,
) -> tuple[AccessScope, ...]:
    """Collect all active direct or role scopes granting a permission."""
    return tuple(
        _direct_scopes_for_permission(user, permission)
        + _role_scopes_for_permission(user, permission)
    )


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
# Purpose: Assemble the coverage and audit-derived smart-alert inputs for the
#   monthly dashboard route. The missing-facts coverage signal is always
#   available; connector audit signals are only read when the caller already
#   passed the VIEW_AUDIT_LOG gate.
# Database/ORM: Read-only channel/fact coverage SELECT plus optional AuditLogORM
#   and connector-run audit scans. No locks or writes.
# Standards: Keeps the route from carrying branch-local audit counters across
#   permission paths; sensitive skipped-row reasons remain controlled by
#   VIEW_SENSITIVE_AUDIT_PAYLOADS.
# Blast Radius: Finance dashboard read surface and audit-observability boundary.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> dataclass
#     consumed by the pure alert builder.
#   - File: backend/ums_smart_revenue/connectors/runs/audit_alerts.py -> failed
#     connector-run status aggregation used here.
# ============================================================================
def _month_smart_alert_audit_signals(
    session: Session,
    *,
    month: str,
    can_view_audit_log: bool,
    include_sensitive_details: bool,
) -> MonthlySmartAlertAuditSignals:
    """Return coverage inputs plus permission-gated audit signals for a month."""
    # FIX: Bounded coverage query — total count plus an ordered, capped
    # sample. The previous shape materialized the full id list, which
    # turns the alert endpoint into an unbounded scan/transfer on bad
    # ingestion months. The alert details (channel_count + sample) keep
    # the same wire shape; only the source of the sample changed.
    (
        missing_fact_channel_count,
        missing_fact_channel_sample,
    ) = missing_revenue_fact_channel_count_and_sample(session, month=month)
    if not can_view_audit_log:
        return MonthlySmartAlertAuditSignals(
            missing_revenue_fact_channel_count=missing_fact_channel_count,
            missing_revenue_fact_channel_sample=missing_fact_channel_sample,
        )

    skipped_source_row_count, skipped_source_rows_by_reason = skipped_source_row_count_and_reasons(
        session,
        month=month,
        include_sensitive_details=include_sensitive_details,
    )
    failed_connector_run_count, failed_connector_runs_by_status = (
        failed_connector_run_count_and_statuses(
            session,
            tenant_id=_resolve_smart_alert_tenant_id(),
            month=month,
        )
    )
    return MonthlySmartAlertAuditSignals(
        missing_revenue_fact_channel_count=missing_fact_channel_count,
        missing_revenue_fact_channel_sample=missing_fact_channel_sample,
        skipped_source_row_count=skipped_source_row_count,
        skipped_source_rows_by_reason=skipped_source_rows_by_reason,
        failed_connector_run_count=failed_connector_run_count,
        failed_connector_runs_by_status=failed_connector_runs_by_status,
    )


# ============================================================================
# Purpose: Read the active, revenue-required channels that have no revenue fact
#   for the month, so the smart-alert builder can emit per-channel coverage
#   gaps. Bounded: returns the total COUNT plus an ordered sample capped at
#   `MISSING_FACT_CHANNEL_SAMPLE_LIMIT` ids, so a bad ingestion month cannot
#   turn the alert endpoint into an unbounded scan/transfer.
# Database/ORM: Read-only LEFT JOIN of YouTubeChannelORM x
#   MonthlyChannelRevenueFactORM (no FOR UPDATE), tenant-scoped, source of
#   truth in PostgreSQL. The count uses `COUNT(*)` server-side; the
#   sample is a separate ordered `LIMIT 20` SELECT.
# Standards: Mirrors month_close_readiness._missing_required_revenue_fact_count
#   exactly (active.is_(True) AND revenue_required.is_(True) AND fact.id IS NULL).
#   No write, no lock, no Neo4j.
# Blast Radius: Finance read surface only; no auth/audit/finance mutation.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
#     shared query shape (count there; count + bounded sample here).
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes
#     (count, sample) for the coverage alert details.
# ============================================================================
def missing_revenue_fact_channel_count_and_sample(
    session: Session,
    *,
    month: str,
    youtube_channel_ids: set[str] | None = None,
) -> tuple[int, list[str]]:
    """Return (count, sample) of active revenue-required channels with no fact for the month.

    `count` is the total matching channels; `sample` is a sorted list of at
    most `MISSING_FACT_CHANNEL_SAMPLE_LIMIT` channel ids. The two values are
    read by two independent queries so a large factless set never materializes
    a full id list on the application side.

    When `youtube_channel_ids` is provided (non-None), the read is scoped to
    those channels — used by the export helper so a company/sector/group
    export never leaks factless channel ids outside the exported scope. When
    omitted (None), the read is tenant-global — the smart-alerts API
    endpoint stays global by design.
    """
    from ums_smart_revenue.finance.smart_alerts import MISSING_FACT_CHANNEL_SAMPLE_LIMIT

    tenant_id = _resolve_smart_alert_tenant_id()
    join_predicates = (
        (MonthlyChannelRevenueFactORM.tenant_id == YouTubeChannelORM.tenant_id)
        & (MonthlyChannelRevenueFactORM.youtube_channel_id == YouTubeChannelORM.youtube_channel_id)
        & (MonthlyChannelRevenueFactORM.tenant_id == tenant_id)
        & (MonthlyChannelRevenueFactORM.month == month),
    )
    where_predicates = [
        YouTubeChannelORM.tenant_id == tenant_id,
        YouTubeChannelORM.active.is_(True),
        YouTubeChannelORM.revenue_required.is_(True),
        MonthlyChannelRevenueFactORM.id.is_(None),
    ]
    if youtube_channel_ids is not None:
        where_predicates.append(YouTubeChannelORM.youtube_channel_id.in_(youtube_channel_ids))
    count_statement = (
        select(literal_column("COUNT(*)", type_=Integer()))
        .select_from(YouTubeChannelORM)
        .outerjoin(MonthlyChannelRevenueFactORM, *join_predicates)
        .where(*where_predicates)
    )
    sample_statement = (
        select(YouTubeChannelORM.youtube_channel_id)
        .select_from(YouTubeChannelORM)
        .outerjoin(MonthlyChannelRevenueFactORM, *join_predicates)
        .where(*where_predicates)
        .order_by(YouTubeChannelORM.youtube_channel_id)
        .limit(MISSING_FACT_CHANNEL_SAMPLE_LIMIT)
    )
    count_value = int(session.execute(count_statement).scalar_one())
    sample_ids = list(session.scalars(sample_statement).all())
    return count_value, sample_ids


# ============================================================================
# Purpose: Read connector normalization audit edges for a finance month and
#   derive the current smart-alert source-row skip signal from the newest
#   relevant connector-run edge only. Filtering the JSON lifecycle in Python
#   keeps the read portable across SQLite tests and PostgreSQL production while
#   the SQL predicates stay tenant/month/event scoped.
# Database/ORM: Read-only SELECT on AuditLogORM / audit_logs; no locks or
#   writes. PostgreSQL remains the source of truth for audit observability.
# Standards: Tenant-scoped via _resolve_smart_alert_tenant_id; a newer clean
#   connector edge clears older ROWS_SKIPPED history; ignores malformed or
#   zero-count audit details instead of making the dashboard fail on legacy
#   audit rows; preserves valid reason counts exactly.
# Blast Radius: Finance dashboard read surface; no finance calculation,
#   authorization, export, ingestion, or audit-write behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits lifecycle=ROWS_SKIPPED with skipped_count/skipped_by_reason.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes the
#     aggregate as SOURCE_ROWS_SKIPPED.
# ============================================================================
def skipped_source_row_count_and_reasons(
    session: Session,
    *,
    month: str,
    include_sensitive_details: bool = True,
) -> tuple[int, dict[str, int]]:
    """Return skipped source rows + skip reasons for one finance month.

    Reads connector `ROWS_SKIPPED` audit edges scoped by tenant,
    `CONNECTOR_JOB_RUN` event type, `FINANCE_MONTH` scope, and the requested
    month. The function returns the newest connector-run signal only — not
    the sum across re-runs — because fact projection is idempotent and each
    connector run emits its own edge for the same month; aggregating across
    runs would over-count stale or duplicate signals (review threads #1 and
    #10). A newer clean connector edge clears older `ROWS_SKIPPED` history.
    Malformed or zero-count rows are tolerated: `skipped_count` and
    `skipped_by_reason` are reconciled via `max()` so the returned pair is
    internally consistent (review thread #8).

    Args:
        session: Active SQLAlchemy session.
        month: Finance month in `YYYY-MM` format (already validated by caller).
        include_sensitive_details: When False, the returned reason breakdown is
            redacted to an empty dict so callers without
            `VIEW_SENSITIVE_AUDIT_PAYLOADS` cannot learn per-reason counts.
            The total `skipped_count` is still returned because the count is
            operational, not sensitive.

    Returns:
        A (count, reasons_by_label) tuple. `reasons_by_label` is always
        returned in deterministic key-sorted order.
    """
    tenant_id = _resolve_smart_alert_tenant_id()
    # FIX: Read only the newest relevant connector edge for the month. If that
    # newest edge is not ROWS_SKIPPED, a clean re-run has superseded the older
    # skipped-row audit history and the dashboard must not show a stale alert.
    details_rows = session.scalars(
        select(AuditLogORM.details)
        .where(
            AuditLogORM.tenant_id == tenant_id,
            AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
            AuditLogORM.scope_type == ScopeType.FINANCE_MONTH.value,
            AuditLogORM.scope_id == month,
        )
        .order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
    ).all()
    latest_details: dict[str, object] | None = None
    for details in details_rows:
        if not isinstance(details, dict):
            continue
        if details.get("lifecycle") != "ROWS_SKIPPED":
            return 0, {}
        latest_details = details
        break
    if latest_details is None:
        return 0, {}
    reason_counts = _skipped_reason_counts_from_details(latest_details)
    skipped_count = _positive_int(latest_details.get("skipped_count"))
    reasons_total = sum(reason_counts.values())
    if skipped_count > 0 and reasons_total > 0:
        effective_count = max(skipped_count, reasons_total)
    elif skipped_count > 0:
        effective_count = skipped_count
    elif reasons_total > 0:
        effective_count = reasons_total
    else:
        effective_count = 0
    if not include_sensitive_details:
        # Redact per-reason breakdown; keep total count visible.
        return effective_count, {}
    return effective_count, dict(sorted(reason_counts.items()))


def _skipped_reason_counts_from_details(details: dict[str, object]) -> dict[str, int]:
    """Extract positive reason counts from one ROWS_SKIPPED audit detail payload."""
    raw_reason_counts = details.get("skipped_by_reason")
    if not isinstance(raw_reason_counts, dict):
        return {}
    reason_counts: dict[str, int] = {}
    for raw_reason, raw_count in raw_reason_counts.items():
        reason = str(raw_reason).strip()
        count = _positive_int(raw_count)
        if reason and count > 0:
            reason_counts[reason] = reason_counts.get(reason, 0) + count
    return reason_counts


def _positive_int(value: object) -> int:
    """Return positive JSON integer-like values; malformed values collapse to zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value)
    return 0


def _resolve_smart_alert_tenant_id() -> UUID:
    """Resolve the request tenant id, mirroring the finance repositories."""
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return UUID(UMS_TENANT_ID)


# ============================================================================
# Purpose: Translate a revenue-read scope string into the AccessScope and
#   channel filter used by the net-revenue, rankings, and recalculation
#   preview routes.
# Database/ORM: None directly; the underlying registry and org index are
#   supplied by the caller and live in the service layer
#   (resolve_revenue_read_scope).
# Standards: Route-boundary shim. Pure transport-layer translation: typed
#   service errors become HTTP errors with the route's primary gate message
#   so unauthorized probes and unauthorized reads are indistinguishable.
# Blast Radius: Finance read scope resolution and audit entity identity.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_scopes.py ->
#       resolve_revenue_read_scope owns the actual scope translation and
#       group registry access.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> PostgreSQL
#       source for channel-group metadata and member channel IDs.
# ============================================================================
def _revenue_read_scope_to_channel_ids(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> tuple[AccessScope, set[str] | None]:
    """Thin shim around resolve_revenue_read_scope that maps service errors to HTTP."""
    from ums_smart_revenue.finance.revenue_scopes import (
        RevenueReadScopeRequestShapeError,
        RevenueReadScopeResolutionError,
        resolve_revenue_read_scope,
    )

    try:
        return resolve_revenue_read_scope(
            scope_type=scope_type,
            scope_id=scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
    except RevenueReadScopeRequestShapeError as exc:
        # FIX (chatgpt-codex-connector review #122): Translate request-shape
        # errors (missing/extra scope_id, unknown scope_type) to 422 so the
        # caller can distinguish a malformed request from an unauthorized
        # read. Only group-validity errors stay normalized to 403.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except RevenueReadScopeResolutionError as exc:
        # FIX (Qodo review #122): Normalize invalid group lookups to HTTP 403
        # instead of 404/422 so a caller cannot probe for group existence,
        # active state, or emptiness. The same 403 is returned when the
        # caller lacks VIEW_REVENUE on every member channel, so unauthorized
        # probes and unauthorized reads are indistinguishable.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_REVENUE.value}",
        ) from exc


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


def audit_record_to_response(record: AuditRecord) -> AuditEventResponse:
    """Serialize an AuditRecord to the typed finance read audit-event response."""
    return AuditEventResponse.model_validate(audit_record_to_api(record))


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
