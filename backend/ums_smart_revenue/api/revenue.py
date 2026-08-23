# ============================================================================
# Purpose: Serve the finance revenue read API (facts, allocation, smart-alerts,
#   monthly summaries, reconciliation, explanation). Routes are thin: they
#   parse input, enforce typed permissions + tenant/scope boundaries, call
#   finance + repository services, and translate typed domain errors into
#   HTTP responses. No finance math or audit writes happen here.
# Database/ORM: Reads/writes via SQLAlchemy repositories
#   (RevenueFactRepository, AdSensePaymentRepository, BankReconciliationRepository,
#   ManualOverrideRepository, FinanceMonthCloseRepository, AllocationRepository);
#   the tenant-scoped smart-alert signal reads (missing-fact coverage, skipped
#   rows) are delegated to finance.smart_alert_signals.
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
#   - File: backend/ums_smart_revenue/api/authz.py -> the shared permission
#     gates; File: backend/ums_smart_revenue/api/dependencies_finance.py ->
#     the shared providers (org index, repositories, audit sinks). Both
#     extracted from this module so sibling route modules stop importing its
#     internals.
#   - File: Docs/12_BACKEND_API_SPEC.md -> endpoint contracts and alert codes.
# ============================================================================
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.authz import raise_missing_permission, require_permission
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
    current_revenue_audit_sink,
    current_revenue_fact_repository,
)
from ums_smart_revenue.api.org_units import current_org_unit_reader
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_log import SqlAlchemyAuditLogRepository
from ums_smart_revenue.auth.audit_service import (
    AuditRecord,
    AuditSink,
    record_audit_event,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.read_snapshot import begin_composed_read_snapshot
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
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
    MonthBankReconciliationSummary,
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
    DeductionComponentPage,
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
from ums_smart_revenue.finance.gap_explanation import (
    MonthGapExplanation,
    build_month_gap_explanation,
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
    MonthNetRevenueSummary,
    NetRevenueValidationError,
    build_month_net_revenue_summary,
    filter_account_allocations_to_scope,
    normalize_net_revenue_currency,
)
from ums_smart_revenue.finance.payment_matching import (
    MonthlyPaymentMatchSummary,
    PaymentMatchValidationError,
    build_monthly_payment_match_summary,
    normalize_payment_match_currency,
)
from ums_smart_revenue.finance.rankings import (
    MonthRankingsSummary,
    RankingsValidationError,
    build_month_rankings,
)
from ums_smart_revenue.finance.recalculation import (
    RevenueRecalculationPreview,
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
from ums_smart_revenue.finance.smart_alert_signals import (
    missing_revenue_fact_channel_count_and_sample,
    previous_month,
    resolve_smart_alert_tenant_id,
    skipped_source_row_count_and_reasons,
)
from ums_smart_revenue.finance.smart_alerts import (
    MonthlySmartAlertAuditSignals,
    MonthlySmartAlertFinanceInputs,
    MonthlySmartAlertSummary,
    MonthlySmartAlertTrendSignals,
    build_monthly_smart_alert_summary,
)
from ums_smart_revenue.org.access_index import load_org_access_index_from_session
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.org.org_units_read import SqlAlchemyOrgUnitReader
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry

router = APIRouter(prefix="/revenue", tags=["revenue"])
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


@dataclass(frozen=True)
class _MonthConnectorSmartAlertAuditContext:
    """Dependencies shared by monthly connector smart-alert self-audit records."""

    audit_sink: AuditSink
    user: UserPrincipal
    month: str
    audit_scope: AccessScope


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


class MonthlySmartAlertItemResponse(BaseModel):
    """Typed smart-alert item returned by the monthly dashboard endpoint."""

    code: str
    severity: str
    message: str
    source: str
    confidence: str
    details: dict[str, object]


class MonthSmartAlertsResponse(BaseModel):
    """Typed monthly smart-alert dashboard response."""

    month: str
    status: str
    highest_severity: str | None
    alert_count: int
    alerts: list[MonthlySmartAlertItemResponse]
    audit_events: list[AuditEventResponse]


class MonthDeductionComponentsResponse(BaseModel):
    """Typed monthly deduction-components response."""

    month: str
    total_count: int
    returned_count: int
    scopes: list[DeductionComponentScopeGroup]
    pagination: DeductionComponentsPagination
    audit_events: list[AuditEventResponse]


class GapExplanationConfidenceResponse(BaseModel):
    """Explain-shape confidence pair carried by gap components and residuals."""

    label: str
    score: str


class GapExplanationComponentResponse(BaseModel):
    """One evidence-backed component of a gap leg."""

    key: str
    label: str
    amount_usd: str
    evidence_count: int
    confidence: GapExplanationConfidenceResponse


class GapExplanationWarningResponse(BaseModel):
    """One non-blocking data caveat attached to the gap explanation."""

    code: str
    message: str


class GapPaymentLegResponse(BaseModel):
    """Payment leg of the composed gap explanation (field order = wire order)."""

    status: str
    youtube_revenue_total_usd: str
    adsense_paid_amount_usd: str
    payment_gap_usd: str | None
    payment_match_status: str
    components: list[GapExplanationComponentResponse]
    unexplained_residual_usd: str | None
    unexplained_residual_confidence: GapExplanationConfidenceResponse
    narrative: str


class GapBankLegResponse(BaseModel):
    """Bank leg of the composed gap explanation (field order = wire order)."""

    status: str
    adsense_paid_amount_usd: str
    bank_received_amount_usd: str
    bank_gap_usd: str | None
    bank_reconciliation_status: str
    components: list[GapExplanationComponentResponse]
    unexplained_residual_usd: str | None
    unexplained_residual_confidence: GapExplanationConfidenceResponse
    narrative: str


class GapMoneyProvenanceEntryResponse(BaseModel):
    """Provenance entry for one numeric gap-explanation field."""

    source: str
    formula: str
    confidence: str
    export_value: str | None


class MonthGapExplanationResponse(BaseModel):
    """Typed composed month gap-explanation response."""

    month: str
    currency: str
    close_status: str
    status: str
    tolerance_usd: str
    payment_leg: GapPaymentLegResponse
    bank_leg: GapBankLegResponse
    warnings: list[GapExplanationWarningResponse]
    money_provenance: dict[str, GapMoneyProvenanceEntryResponse]
    narrative: str
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


@router.get("/channels/{channel_id}/authorization-check", response_model=AuthorizationCheckResponse)
def check_channel_revenue_authorization(
    channel_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> AuthorizationCheckResponse:
    """Check whether the caller holds VIEW_REVENUE permission for the given channel."""
    target_scope = AccessScope.channel(channel_id)
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
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
# Purpose: Data-access + composition step for the recalculation preview,
#   extracted out of the route handler (thin-orchestration rule): on a dry
#   run, begin the composed-read snapshot; fetch facts and overrides once and
#   build the readiness preview.
# Database/ORM: Reads facts and overrides via the RevenueFact and
#   ManualOverride repositories. dry_run=true begins the platform session's
#   REPEATABLE READ composed-read snapshot first (db/read_snapshot.py) — the
#   preview is a composed two-source read, so an override committing between
#   the fetches can no longer produce a readiness decision from sources that
#   never coexisted. dry_run=false deliberately does NOT begin it: the same
#   request continues into the committed-allocation writer, and wiring
#   reads-then-write into one REPEATABLE READ transaction is the recorded
#   write-path residual (upsert conflicts would abort rather than degrade —
#   the same ruling that keeps the explain POST unwired). No writes here
#   either way.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin. On a dry run the scoped selection (org units via the
#   snapshot index, groups via the registry roster —
#   _snapshot_selection_channel_ids) AND the channel_company readiness map
#   re-resolve on the snapshot — attribution is money-adjacent, so a channel
#   moved between org units or dropped from a group roster mid-request is
#   never previewed under its former scope and COMPANY_UNMAPPED readiness is
#   judged against the same database state as the facts; the deny-only grant
#   re-check covers org-unit and channel targets. The WRITE branch keeps the
#   TENANT-lane index for both: its preflight preview must mirror the commit
#   engine's fail-closed COMPANY_UNMAPPED behavior from the SAME index
#   within the same request
#   (preview/commit parity is load-bearing there). Source ValidationErrors
#   propagate untouched for the route's 422 translation.
# Blast Radius: Every readiness number the recalculation preview serves and
#   the write branch's pre-flight gate. Read-only — the route owns the
#   write, the audit event, and the response envelope.
# Connections:
#   - File: backend/ums_smart_revenue/finance/recalculation.py -> the pure
#     preview builder.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the dry-run
#     snapshot begun before the first fetch.
# ============================================================================
def _load_recalculation_preview(
    *,
    payload: RevenueRecalculationRequest,
    user: UserPrincipal,
    target_scope: AccessScope,
    channel_ids: set[str] | None,
    org_index: OrgAccessIndex,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    override_repository: SqlAlchemyManualOverrideRepository,
) -> RevenueRecalculationPreview:
    """Fetch facts and overrides (dry runs: inside the snapshot), build the preview."""
    selection_channel_ids = channel_ids
    channel_company = org_index.channel_company
    if payload.dry_run:
        # FIX: One MVCC snapshot for both source reads below on the dry-run
        # preview — an override or fact committing between them can no longer
        # tear the readiness decision — and for the org attribution: the
        # selection set and the COMPANY_UNMAPPED readiness map re-resolve
        # from the snapshot index, so a channel moved between org units
        # mid-request is never previewed under its former scope (REPEATABLE
        # READ on Postgres; db/read_snapshot.py holds the ruling). The write
        # branch stays READ COMMITTED on the tenant index by the recorded
        # write-path ruling (preview/commit parity within one request).
        begin_composed_read_snapshot(platform_session)
        snapshot_index = load_org_access_index_from_session(platform_session)
        # Deny-only: a sector-granted caller whose target unit was reparented
        # out of the grant — or whose target channel was moved out of the
        # granted unit — between the gate and the snapshot must not preview
        # snapshot-era finance data under gate-era containment.
        _require_snapshot_org_scope_access(
            user,
            permissions=(Permission.VIEW_REVENUE,),
            target_scope=target_scope,
            org_index=snapshot_index,
        )
        selection_channel_ids = _snapshot_selection_channel_ids(
            user,
            permissions=(Permission.VIEW_REVENUE,),
            target_scope=target_scope,
            authorized_channel_ids=channel_ids,
            platform_session=platform_session,
            org_index=snapshot_index,
        )
        channel_company = snapshot_index.channel_company
    facts = revenue_repository.list_month_facts(
        month=payload.month,
        youtube_channel_ids=selection_channel_ids,
    )
    overrides = override_repository.list_month_overrides(
        month=payload.month,
        youtube_channel_ids=selection_channel_ids,
    )
    return build_recalculation_preview(
        month=payload.month,
        allocation_method=payload.allocation_method,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        currency=payload.currency,
        dry_run=payload.dry_run,
        facts=facts,
        manual_overrides=overrides,
        # company_level readiness: the dry run judges COMPANY_UNMAPPED from
        # the snapshot index; the write branch's preflight mirrors the commit
        # engine's fail-closed path from the same tenant index it commits
        # with.
        channel_company=channel_company,
        # Pass the scoped channel set so verified channels without fact
        # rows are also checked against the company mapping, preventing
        # false READY_FOR_REVIEW before a company_level commit.
        verified_channel_ids=(
            frozenset(selection_channel_ids) if selection_channel_ids is not None else None
        ),
    )


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
#   audit carries the final write_status. No secrets, no per-line dump. The
#   dry-run preview reads run inside one REPEATABLE READ composed-read snapshot
#   on Postgres, begun by _load_recalculation_preview after the gates (the
#   handler never touches the session); the write branch stays READ COMMITTED
#   by the recorded write-path ruling.
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
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
    require_permission(user, Permission.CHANGE_ALLOCATION_RULE, month_scope)
    if not payload.dry_run:
        require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)

    normalized_write_method: str | None = None
    write_idempotency_key: str | None = None
    if not payload.dry_run:
        normalized_write_method, write_idempotency_key = _validate_recalculation_write_request(
            payload
        )

    try:
        preview = _load_recalculation_preview(
            payload=payload,
            user=user,
            target_scope=target_scope,
            channel_ids=channel_ids,
            org_index=org_index,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            override_repository=override_repository,
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
    require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
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


# ============================================================================
# Purpose: List every stored revenue fact for one channel and month — the
#   raw multi-source rows the reconciliation preview compares. Read-only.
# Database/ORM: One RevenueFact repository read on the platform-lane session;
#   appends a REVENUE_VIEWED audit event through the revenue audit sink. No
#   locks or writes to finance rows.
# Standards: Single VIEW_REVENUE gate at channel scope through the org-access
#   index; denial precedes the source read. The read happens inside the
#   REPEATABLE READ composed-read snapshot begun by _load_channel_month_facts
#   after the gate (the repository read is two statements — the
#   active-channel guard, then the facts select — the same guard-then-select
#   shape whose single-select exemption was disproven on the
#   reconciliation-preview route); the handler itself never touches the
#   session (thin-orchestration rule).
# Blast Radius: Channel-level facts read surface; 404 on unknown
#   channel/month, 422 on malformed month.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> the
#     two-statement repository read under the snapshot.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gate and the source read.
#   - File: Docs/12_BACKEND_API_SPEC.md -> channel facts listing contract.
# ============================================================================
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return all revenue facts recorded for a channel in a given month."""
    target_scope = AccessScope.channel(channel_id)
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts = _load_channel_month_facts(
            month=month,
            channel_id=channel_id,
            user=user,
            permissions=(Permission.VIEW_REVENUE,),
            platform_session=platform_session,
            repository=repository,
        )
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


# ============================================================================
# Purpose: Data-access step shared by the per-channel facts listing and the
#   reconciliation preview, extracted out of the route handlers
#   (thin-orchestration rule): begin the composed-read snapshot and fetch the
#   channel's facts.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads via the RevenueFact
#   repository. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so each
#   route's permission gates always run first: denial must precede any
#   transaction begin. These endpoints were originally EXEMPT on a
#   single-select premise, disproven in review (codex): list_channel_month_facts
#   issues TWO statements — the active-channel guard, then the facts select —
#   so a channel deactivated between them could pair a stale guard decision
#   with the facts; under the snapshot the guard state and the facts provably
#   coexist. The channel target is then re-checked deny-only against the
#   snapshot index (_require_snapshot_org_scope_access with each route's own
#   gate permissions): a caller admitted through an inherited sector/company
#   grant whose channel moved out of the granting unit mid-request 403s
#   instead of receiving snapshot-era facts under gate-era containment, while
#   direct channel grants keep passing on scope identity. Not-found and
#   ValidationErrors propagate untouched for the routes' 404/422 translation.
# Blast Radius: The guard/facts coherence and stale-containment refusal of
#   both per-channel reads. Read-only — each route owns its build and audit
#   event.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> the
#     two-statement repository read this snapshot makes coherent.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first statement.
# ============================================================================
def _load_channel_month_facts(
    *,
    month: str,
    channel_id: str,
    user: UserPrincipal,
    permissions: tuple[Permission, ...],
    platform_session: Session,
    repository: SqlAlchemyRevenueFactRepository,
) -> list[RevenueFactEntry]:
    """Begin the composed-read snapshot, re-check the channel, fetch its facts."""
    # FIX: One MVCC snapshot for the active-channel guard and the facts select
    # inside list_channel_month_facts — the guard decision and the facts it
    # authorizes can no longer come from different database states
    # (REPEATABLE READ on Postgres; db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    # Deny-only: a caller admitted via an inherited org grant must not be
    # served after the channel moved out of the granting unit mid-request.
    _require_snapshot_org_scope_access(
        user,
        permissions=permissions,
        target_scope=AccessScope.channel(channel_id),
        org_index=load_org_access_index_from_session(platform_session),
    )
    return repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)


# ============================================================================
# Purpose: Serve the per-channel multi-source reconciliation preview for one
#   month — every stored fact for the channel/month compared source-by-source.
#   Read-only; never mutates finance numbers.
# Database/ORM: One RevenueFact repository read on the platform-lane
#   session; appends a REVENUE_VIEWED audit event through the revenue audit
#   sink. No locks or writes to finance rows.
# Standards: Two-permission gate — VIEW_REVENUE + VIEW_CONFIDENCE at channel
#   scope through the org-access index; denial precedes the source read.
#   The read happens inside the REPEATABLE READ composed-read snapshot begun
#   by _load_channel_month_facts after the gates (the repository read is two
#   statements — guard, then select — so the single-select exemption this
#   route once carried was unsound); the handler itself never touches the
#   session (thin-orchestration rule).
# Blast Radius: Channel-level reconciliation read surface; 404 on unknown
#   channel/month, 422 on malformed month.
# Connections:
#   - File: backend/ums_smart_revenue/finance/reconciliation.py -> the pure
#     preview builder.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gates and the source read.
#   - File: Docs/12_BACKEND_API_SPEC.md -> reconciliation-preview contract.
# ============================================================================
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Build and return the multi-source reconciliation preview for a channel and month."""
    target_scope = AccessScope.channel(channel_id)
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    try:
        facts = _load_channel_month_facts(
            month=month,
            channel_id=channel_id,
            user=user,
            permissions=(Permission.VIEW_REVENUE, Permission.VIEW_CONFIDENCE),
            platform_session=platform_session,
            repository=repository,
        )
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


# ============================================================================
# Purpose: Data-access step for the month reconciliation issue queue,
#   extracted out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot, narrow the visible set on it, fetch one
#   channel-id page and its facts.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), re-reads the org-access index on it, then
#   reads the channel-id page and the page's facts via the RevenueFact
#   repository. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's grant checks always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). The gate-time visible set is
#   then INTERSECTED with the same covered-set derivation re-run on the
#   snapshot index, deny-only (both-states rule): a channel moved out of the
#   caller's granted unit between the gate and the snapshot is dropped
#   before paging instead of having its snapshot-era facts served under
#   gate-era containment, and a channel moved in was never gate-covered and
#   stays out. The route's empty-scope early return never reaches this
#   loader; a selection that empties ON THE SNAPSHOT returns an empty page
#   without touching the repository (empty queue, not 403 — the same
#   contract as the route's gate-time empty). The snapshot-EFFECTIVE scope
#   set is returned so the route's audit reports the scope that actually
#   served the page — auditing the stale gate-time set would claim a
#   channel was in scope after the recheck removed it. Source
#   ValidationErrors propagate untouched for the route's 422 translation.
# Blast Radius: The issue queue's page coherence, which channels' facts it
#   may page, and the audited scope count. Deny-only — the snapshot
#   intersect can only shrink the selection.
# Connections:
#   - File: backend/ums_smart_revenue/finance/reconciliation.py -> the pure
#     issue-queue builder consuming the page's facts.
#   - File: backend/ums_smart_revenue/org/access_index.py -> the snapshot
#     index the covered sets re-derive from.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_reconciliation_issue_page(
    *,
    month: str,
    user: UserPrincipal,
    visible_channel_ids: set[str] | None,
    limit: int,
    offset: int,
    platform_session: Session,
    repository: SqlAlchemyRevenueFactRepository,
) -> tuple[list[str], list[RevenueFactEntry], bool, set[str] | None]:
    """Begin the composed-read snapshot, narrow the scope on it, fetch one page.

    Returns ``(channel_ids_for_page, facts, has_more, effective_scope_channel_ids)``
    where the last element is the snapshot-narrowed scope the page was
    actually selected from (``None`` for global callers).
    """
    # FIX: One MVCC snapshot for the channel-id page and the facts read below —
    # a fact committing between them can no longer tear the issue queue against
    # its own pagination (REPEATABLE READ on Postgres; db/read_snapshot.py
    # holds the ruling).
    begin_composed_read_snapshot(platform_session)
    # Deny-only: re-derive the covered sets on the snapshot index and
    # intersect with the gate-time visible set, so a channel moved out of the
    # caller's granted unit mid-request is dropped before paging.
    snapshot_index = load_org_access_index_from_session(platform_session)
    snapshot_visible_channel_ids = _intersect_channel_sets(
        _authorized_channel_ids_for_permission(user, Permission.VIEW_REVENUE, snapshot_index),
        _authorized_channel_ids_for_permission(user, Permission.VIEW_CONFIDENCE, snapshot_index),
    )
    page_scope_channel_ids = _intersect_channel_sets(
        visible_channel_ids, snapshot_visible_channel_ids
    )
    if page_scope_channel_ids is not None and not page_scope_channel_ids:
        return [], [], False, page_scope_channel_ids
    page_size = limit + 1
    page_channel_ids = repository.list_month_channel_ids(
        month=month,
        youtube_channel_ids=page_scope_channel_ids,
        limit=page_size,
        offset=offset,
    )
    channel_ids_for_page = page_channel_ids[:limit]
    facts = repository.list_month_facts(
        month=month,
        youtube_channel_ids=set(channel_ids_for_page),
    )
    return channel_ids_for_page, facts, len(page_channel_ids) > limit, page_scope_channel_ids


# ============================================================================
# Purpose: Serve the paginated month reconciliation issue queue — every
#   authorized channel's facts scanned for cross-source issues, ordered for
#   finance triage. Read-only; never mutates finance numbers.
# Database/ORM: Channel-id page + facts reads via the RevenueFact repository
#   on the platform-lane session; appends a REVENUE_VIEWED audit event through
#   the revenue audit sink. No locks or writes to finance rows.
# Standards: VIEW_REVENUE + VIEW_CONFIDENCE, scope-mapped: a caller with no
#   relevant grant at all is rejected, while a scoped grant that currently
#   maps to zero channels sees an empty queue (not 403). Both source reads
#   happen inside one REPEATABLE READ composed-read snapshot on Postgres
#   (db/read_snapshot.py) begun after the grant checks, so a fact committing
#   mid-read cannot tear the queue against its own pagination. The channel
#   authorization itself runs in-memory over the tenant-lane org-access index
#   before the snapshot (laning is an authorization boundary — platform data
#   never grants); the loader then re-derives the covered sets on the
#   snapshot index and intersects them with that gate-time set, deny-only,
#   so a channel moved out of the granted unit mid-request is dropped before
#   paging.
# Blast Radius: Finance triage read surface; pagination contract
#   (limit/offset/next_offset/has_more) feeds the frontend issue queue.
# Connections:
#   - File: backend/ums_smart_revenue/finance/reconciliation.py -> the pure
#     issue-queue builder.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the grant checks and the source reads.
#   - File: Docs/12_BACKEND_API_SPEC.md -> reconciliation-issues contract.
# ============================================================================
@router.get("/months/{month}/reconciliation-issues")
def list_month_reconciliation_issues(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """Return the reconciliation issue queue for the caller's channels."""
    # Reject only when the caller has no relevant grant at all; a caller whose
    # scoped grant currently maps to zero channels (e.g. sector/company with
    # no active mapping) should see an empty queue, not 403.
    if user.disabled or not _granted_scopes_for_permission(user, Permission.VIEW_REVENUE):
        raise_missing_permission(Permission.VIEW_REVENUE)
    if not _granted_scopes_for_permission(user, Permission.VIEW_CONFIDENCE):
        raise_missing_permission(Permission.VIEW_CONFIDENCE)

    # The channel authorization below runs in-memory over the org-access index
    # (loaded on the TENANT lane) before the composed-read snapshot, which
    # _load_month_reconciliation_issue_page begins afterwards: laning is an
    # authorization boundary and platform data never grants. The loader
    # re-derives these covered sets on the snapshot index and intersects them
    # with this gate-time set, deny-only.
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
        (
            channel_ids_for_page,
            facts,
            has_more,
            effective_scope_channel_ids,
        ) = _load_month_reconciliation_issue_page(
            month=month,
            user=user,
            visible_channel_ids=visible_channel_ids,
            limit=limit,
            offset=offset,
            platform_session=platform_session,
            repository=repository,
        )
    except RevenueFactValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

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
            # The snapshot-EFFECTIVE scope, not the gate-time set: the loader's
            # deny-only recheck may have narrowed the selection, and the audit
            # must not claim a channel was in scope after it was removed.
            "scoped_channel_count": (
                len(effective_scope_channel_ids)
                if effective_scope_channel_ids is not None
                else None
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


# ============================================================================
# Purpose: Data-access + composition step for the monthly payment match,
#   extracted out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot, fetch both sources once, and build the summary.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads via the RevenueFact and
#   AdSensePayment repositories. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). Source ValidationErrors
#   propagate untouched for the route's 422 translation.
# Blast Radius: Every number the payment-match endpoint serves. Read-only —
#   the route owns the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/payment_matching.py -> the pure
#     summary builder and currency normalization.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_payment_match(
    *,
    month: str,
    currency: str,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    payment_repository: SqlAlchemyAdSensePaymentRepository,
) -> MonthlyPaymentMatchSummary:
    """Begin the composed-read snapshot, fetch both sources, build the summary."""
    # FIX: One MVCC snapshot for both source reads below — a payment or fact
    # committing between them can no longer tear the composed totals
    # (REPEATABLE READ on Postgres; db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    normalized_currency = normalize_payment_match_currency(currency)
    facts = revenue_repository.list_month_facts(month=month)
    payments = payment_repository.list_month_payments(month=month)
    return build_monthly_payment_match_summary(
        month=month,
        facts=facts,
        payments=payments,
        currency=normalized_currency,
    )


# ============================================================================
# Purpose: Serve the monthly payment-match summary — YouTube revenue facts
#   compared against AdSense payments for one month, with the match status
#   and self-audit trail. Read-only; never mutates finance numbers.
# Database/ORM: Reads via the RevenueFact and AdSensePayment repositories on
#   the platform-lane session; appends REVENUE_VIEWED + PAYMENT_VIEWED audit
#   events through the revenue audit sink. No locks or writes to finance rows.
# Standards: Two-permission gate — VIEW_REVENUE at global scope plus
#   VIEW_FINALIZED_PAYMENTS at finance-month scope; denial precedes any source
#   read. Both source reads happen inside one REPEATABLE READ composed-read
#   snapshot on Postgres (db/read_snapshot.py) begun by
#   _load_month_payment_match after the gates, so a payment or fact
#   committing mid-read cannot tear the composed totals; the handler itself
#   never touches the session (thin-orchestration rule).
# Blast Radius: Finance dashboard read surface; the payment-match wire
#   contract feeds the Command Center and the smart-alerts builder reuses the
#   same summary shape.
# Connections:
#   - File: backend/ums_smart_revenue/finance/payment_matching.py -> the pure
#     summary builder and currency normalization.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gates and the source reads.
#   - File: Docs/12_BACKEND_API_SPEC.md -> payment-match wire contract.
# ============================================================================
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Compare monthly YouTube revenue facts against AdSense payments."""
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)
    try:
        summary = _load_month_payment_match(
            month=month,
            currency=currency,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            payment_repository=payment_repository,
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
# Purpose: Data-access + composition step for the monthly smart alerts,
#   extracted out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot, fetch every finance source and the missing-fact
#   coverage pair once, gather the audit-gated tenant-lane signals, and build
#   the prioritized alert summary.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads facts (both months), payments,
#   bank entries, overrides, close status, and the coverage pair on that
#   snapshot; the audit-derived signals read through the TENANT-lane
#   `session` and deliberately stay outside it (their laning is an
#   authorization boundary). No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). Source ValidationErrors
#   propagate untouched for the route's 422 translation.
# Blast Radius: Every alert the smart-alerts endpoint serves. Read-only —
#   the route owns the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> the pure
#     alert builder and its input dataclasses.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_smart_alerts(
    *,
    month: str,
    session: Session,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    payment_repository: SqlAlchemyAdSensePaymentRepository,
    bank_repository: SqlAlchemyBankReconciliationRepository,
    override_repository: SqlAlchemyManualOverrideRepository,
    close_repository: SqlAlchemyFinanceMonthCloseRepository,
    can_view_audit_log: bool,
    include_sensitive_details: bool,
) -> tuple[MonthlySmartAlertSummary, MonthlySmartAlertAuditSignals]:
    """Begin the composed-read snapshot, fetch every source, build the alert summary.

    Returns ``(summary, audit_signals)`` — the route needs the signals again
    for its AUDIT_LOG_VIEWED self-audit details.
    """
    # FIX: One MVCC snapshot for every finance source read below (facts, both
    # months, payments, bank entries, overrides, close, missing-fact coverage)
    # — a close committing mid-read can no longer suppress MONTH_NOT_LOCKED
    # against pre-lock totals, and no writer can tear the cross-source alert
    # inputs (REPEATABLE READ on Postgres; db/read_snapshot.py holds the
    # ruling). The coverage query is NOT audit-gated, so it reads through the
    # platform snapshot like every other finance source; only the audit-derived
    # signals read through the TENANT-lane `session` and deliberately stay
    # outside this snapshot: their laning is an authorization boundary.
    begin_composed_read_snapshot(platform_session)
    facts = revenue_repository.list_month_facts(month=month)
    previous_facts = revenue_repository.list_month_facts(month=previous_month(month))
    payments = payment_repository.list_month_payments(month=month)
    bank_entries = bank_repository.list_month_entries(month=month)
    manual_overrides = override_repository.list_month_overrides(month=month)
    close = close_repository.get(month)
    (
        missing_fact_channel_count,
        missing_fact_channel_sample,
    ) = missing_revenue_fact_channel_count_and_sample(platform_session, month=month)
    audit_signals = _month_smart_alert_audit_signals(
        session,
        month=month,
        missing_fact_channel_count=missing_fact_channel_count,
        missing_fact_channel_sample=missing_fact_channel_sample,
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
    return summary, audit_signals


# ============================================================================
# Purpose: Serve the monthly smart-alerts dashboard endpoint. Aggregates
#   cross-domain finance health signals (payment match, bank reconciliation,
#   coverage gap, audit-derived skipped source rows, audit-derived failed
#   connector runs, overrides, MoM revenue anomaly, close status) into a
#   prioritized alert summary + self-audit trail. Read-only; never mutates
#   finance numbers.
# Database/ORM: Reads via RevenueFact/AdSensePayment/BankReconciliation/
#   ManualOverride/FinanceMonthClose repositories plus a tenant-scoped
#   AuditLogORM scan for ROWS_SKIPPED and the audit-log repository for
#   FINISHED/PROJECTION_FAILED connector-run edges.
# Standards: smart-alerts four-permission gate (VIEW_REVENUE/VIEW_CONFIDENCE
#   global + VIEW_FINALIZED_PAYMENTS/VIEW_BANK_RECONCILIATION month-scoped).
#   Audit-derived inputs require VIEW_AUDIT_LOG; without it the alert is
#   omitted (return 0,{}) so finance viewers do not bypass the audit gate.
#   Per-reason breakdown requires VIEW_SENSITIVE_AUDIT_PAYLOADS; without it
#   the count is returned but the breakdown is redacted, mirroring audit.py.
#   Finance sources — including the non-audit-gated missing-fact coverage
#   pair — are read inside one REPEATABLE READ composed-read snapshot on
#   Postgres (db/read_snapshot.py), begun by _load_month_smart_alerts after
#   the gates, so the close status and coverage always pair with the totals
#   of the same snapshot; the tenant-lane audit signals deliberately stay
#   outside it (authorization laning wins), and the handler itself never
#   touches the session (thin-orchestration rule).
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
@router.get("/months/{month}/smart-alerts", response_model=MonthSmartAlertsResponse)
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> MonthSmartAlertsResponse:
    """Aggregate cross-domain health signals for a month into a prioritized smart-alert summary."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    require_permission(user, Permission.VIEW_REVENUE, global_scope)
    require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
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
        summary, audit_signals = _load_month_smart_alerts(
            month=month,
            session=session,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            payment_repository=payment_repository,
            bank_repository=bank_repository,
            override_repository=override_repository,
            close_repository=close_repository,
            can_view_audit_log=can_view_audit_log,
            include_sensitive_details=include_sensitive_details,
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
                context=_MonthConnectorSmartAlertAuditContext(
                    audit_sink=audit_sink,
                    user=user,
                    month=month,
                    audit_scope=audit_scope,
                ),
                audit_signals=audit_signals,
                include_sensitive_details=include_sensitive_details,
            )
        )
    summary_api["audit_events"] = [audit_record_to_api(r) for r in audit_records]
    return MonthSmartAlertsResponse.model_validate(summary_api)


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
    context: _MonthConnectorSmartAlertAuditContext,
    audit_signals: MonthlySmartAlertAuditSignals,
    include_sensitive_details: bool,
) -> AuditRecord:
    """Record that a monthly smart-alert response read connector audit signals."""
    return record_audit_event(
        sink=context.audit_sink,
        actor=context.user,
        event_type=AuditEventType.AUDIT_LOG_VIEWED,
        entity_type="audit_log_page",
        entity_id=f"{context.month}:connector_smart_alerts",
        scope=context.audit_scope,
        details=_month_connector_smart_alert_audit_details(
            month=context.month,
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
# Purpose: Data-access step for the month deduction-components page,
#   extracted out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot and fetch the filtered page.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads via
#   SqlAlchemyDeductionComponentRepository.list_month_components_page — a
#   THREE-statement composition (COUNT, grouped scope totals, page rows)
#   that a mid-read ingestion commit could otherwise tear against itself
#   (total_count vs page vs scope sums vs has_more). No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin. Source ValidationErrors propagate untouched for the
#   route's 422 translation.
# Blast Radius: The deduction-evidence page's internal coherence. Read-only —
#   the route owns the grouping and the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> the
#     three-statement page read this snapshot makes coherent.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first statement.
# ============================================================================
def _load_month_deduction_components_page(
    *,
    month: str,
    component_kind: str | None,
    scope_kind: str | None,
    scope_id: str | None,
    limit: int,
    offset: int,
    platform_session: Session,
    repository: SqlAlchemyDeductionComponentRepository,
) -> DeductionComponentPage:
    """Begin the composed-read snapshot, fetch the filtered deduction page."""
    # FIX: One MVCC snapshot for the COUNT, the grouped scope totals, and the
    # page rows inside list_month_components_page — an ingestion commit
    # between those statements can no longer tear the page against its own
    # totals (REPEATABLE READ on Postgres; db/read_snapshot.py holds the
    # ruling).
    begin_composed_read_snapshot(platform_session)
    return repository.list_month_components_page(
        month=month,
        component_kind=component_kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        limit=limit,
        offset=offset,
    )


# ============================================================================
# Purpose: Read-only per-month deduction-evidence view, grouped by scope
#   (CHANNEL/ACCOUNT/PAYMENT). Surfaces the typed components PR-A ingested; never
#   writes, never triggers ingestion, never returns raw_payload.
# Database/ORM: Reads deduction_components via SqlAlchemyDeductionComponentRepository
#   inside the composed-read snapshot begun by
#   _load_month_deduction_components_page after the gates (the page read is
#   three statements; the handler never touches the session).
# Standards: smart-alerts four-permission auth; audit events match filtered
#   evidence scopes; month validation -> 422; offset/limit pagination.
# Blast Radius: Finance read (deduction evidence). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> repo.
#   - File: backend/ums_smart_revenue/finance/deduction_components.py -> to_api().
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gates and the page read.
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
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
    require_permission(user, Permission.VIEW_REVENUE, global_scope)
    require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        page = _load_month_deduction_components_page(
            month=month,
            component_kind=component_kind,
            scope_kind=scope_kind,
            scope_id=scope_id,
            limit=limit,
            offset=offset,
            platform_session=platform_session,
            repository=repository,
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
        raise_missing_permission(Permission.VIEW_REVENUE)

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
        raise_missing_permission(Permission.VIEW_REVENUE)
    return {"scopes": [option.to_api() for option in options]}


# ============================================================================
# Purpose: Data-access + composition step for the scoped monthly net-revenue
#   summary, extracted out of the route handler (thin-orchestration rule):
#   begin the composed-read snapshot, re-resolve org-derived selection on it,
#   fetch every money source once, and build the summary.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads facts, overrides, deduction
#   components, the allocation resolver's close probe + committed-run
#   selection, and — for org-unit and channel scopes — the snapshot
#   org-access index (grant re-check + member selection), with GROUP
#   membership re-read via the registry on the same snapshot: every input
#   comes from ONE MVCC snapshot, so a lock committing mid-read cannot pair
#   a fresh LOCKED probe with an older in-snapshot committed run. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). Scoped member selection is
#   ATTRIBUTION, so it re-resolves on the snapshot intersected with the
#   gate-time set (_snapshot_selection_channel_ids: org units via the index,
#   groups via the registry roster) — a channel moved between org units or
#   dropped from the roster mid-request is never served under its former
#   scope — and grant coverage is re-asserted deny-only against the same
#   snapshot index (_require_snapshot_org_scope_access) so a reparented
#   target unit, or a channel target moved out of its granting unit, 403s
#   instead of serving snapshot-era data under gate-era containment. Source
#   ValidationErrors propagate untouched for the route's 422 translation.
# Blast Radius: Every number the net-revenue endpoint serves. Read-only —
#   the route owns the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/net_revenue.py -> the pure
#     summary builder and the scope-leak allocation filter.
#   - File: backend/ums_smart_revenue/finance/account_allocation_read.py ->
#     resolve_month_account_allocation (committed snapshot for LOCKED months).
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_net_revenue(
    *,
    month: str,
    currency: str,
    user: UserPrincipal,
    target_scope: AccessScope,
    channel_ids: set[str] | None,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    override_repository: SqlAlchemyManualOverrideRepository,
    deduction_component_repository: SqlAlchemyDeductionComponentRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    committed_repository: SqlAlchemyCommittedAllocationRepository,
) -> tuple[MonthNetRevenueSummary, AllocationProvenance, str]:
    """Begin the composed-read snapshot, fetch the money sources, build the summary.

    Returns ``(summary, allocation_provenance, normalized_currency)``.
    """
    # FIX: One MVCC snapshot for every platform-lane money read below — facts,
    # overrides, deduction components, the committed-allocation read or its
    # live-compute fallback, and the org-derived member selection — so a writer
    # or an org move committing mid-read can no longer tear the scoped totals
    # (REPEATABLE READ on Postgres; db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    normalized_currency = normalize_net_revenue_currency(currency)
    snapshot_org_index: OrgAccessIndex | None = None
    if target_scope.type is not ScopeType.GLOBAL:
        snapshot_org_index = load_org_access_index_from_session(platform_session)
        # Deny-only: a sector-granted caller whose target unit was reparented
        # out of the grant — or whose target channel was moved out of the
        # granted unit — between the gate and the snapshot must not receive
        # snapshot-era finance data under gate-era containment. (The helper
        # gates itself to the recheck scope types; GROUP members are
        # permission-filtered in the selection helper instead.)
        _require_snapshot_org_scope_access(
            user,
            permissions=(Permission.VIEW_REVENUE, Permission.VIEW_CONFIDENCE),
            target_scope=target_scope,
            org_index=snapshot_org_index,
        )
    selection_channel_ids = _snapshot_selection_channel_ids(
        user,
        permissions=(Permission.VIEW_REVENUE, Permission.VIEW_CONFIDENCE),
        target_scope=target_scope,
        authorized_channel_ids=channel_ids,
        platform_session=platform_session,
        org_index=snapshot_org_index,
    )
    is_global_scope = target_scope == AccessScope.global_scope()
    facts = revenue_repository.list_month_facts(
        month=month,
        youtube_channel_ids=selection_channel_ids,
    )
    overrides = override_repository.list_month_overrides(
        month=month,
        youtube_channel_ids=selection_channel_ids,
    )
    deduction_components = deduction_component_repository.list_month_components(
        month=month,
        youtube_channel_ids=selection_channel_ids,
        component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
    )
    # FIX: the close probe inside the resolver reads through the SNAPSHOT
    # session — a lock committing after this snapshot began can no longer
    # pair a fresh LOCKED probe with an older in-snapshot committed run and
    # mislabel that stale run as the locked allocation; close status, run
    # selection, and live inputs all come from one MVCC snapshot.
    account_result, allocation_provenance = resolve_month_account_allocation(
        month=month,
        session=platform_session,
        deduction_repository=deduction_component_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        committed_repository=committed_repository,
    )
    # FIX: the account allocation resolves month-wide (live compute or the
    # committed snapshot), so a scoped read must drop allocation lines for
    # channels outside the resolved selection before they reach the summary
    # builder; otherwise a caller authorized for one company/sector/channel
    # would receive other channels' allocation-derived rows and totals.
    # selection_channel_ids is None for global reads, which pass through.
    scoped_account_lines = filter_account_allocations_to_scope(
        account_result.lines, selection_channel_ids
    )
    summary = build_month_net_revenue_summary(
        month=month,
        facts=facts,
        manual_overrides=overrides,
        deduction_components=deduction_components,
        account_allocations=scoped_account_lines,
        unallocated_account_issues=(account_result.unallocated if is_global_scope else None),
    )
    return summary, allocation_provenance, normalized_currency


# ============================================================================
# Purpose: Return the scoped monthly net-revenue summary, including
#   account-allocated net-applicable deductions on the missing-net path only.
# Database/ORM: Reads revenue facts, manual overrides, deduction components,
#   and channel-account links via _load_month_net_revenue; no writes.
# Standards: Enforce revenue/confidence/payment access before data reads;
#   the composed-read snapshot begins inside the loader after the gates (the
#   handler never touches the session — thin-orchestration rule); global-only
#   unallocated-account surface; dual REVENUE_VIEWED/PAYMENT_VIEWED
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
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
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
    # FIX: Derive the global-surface gate and audit entity ids from the resolved
    # AccessScope, not the raw scope_type/scope_id query strings. The permission
    # checks above already run on the normalized target_scope, so keying the
    # global-only unallocated surface and the audit ids off the raw strings would
    # diverge (e.g. " global " authorizes as global but would be denied the
    # surface and write a malformed entity id).
    normalized_scope_type = target_scope.type.value
    normalized_scope_id = target_scope.id or "global"
    try:
        summary, allocation_provenance, normalized_currency = _load_month_net_revenue(
            month=month,
            currency=currency,
            user=user,
            target_scope=target_scope,
            channel_ids=channel_ids,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            override_repository=override_repository,
            deduction_component_repository=deduction_component_repository,
            link_repository=link_repository,
            committed_repository=committed_repository,
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
# Purpose: Data-access + composition step for the month rankings, extracted
#   out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot, re-resolve org attribution on it, fetch every
#   money source once, and roll the per-channel summary up into rankings.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads facts, overrides, deduction
#   components, AND the snapshot org-access index — the channel->company/
#   sector maps both select the scoped channel set and group the roll-up, so
#   they must come from the same snapshot as the money they attribute. The
#   allocation resolver's close probe + committed-run selection also read
#   through the snapshot (a mid-read lock cannot pair a fresh LOCKED probe
#   with an older in-snapshot run); only the org/channel display-NAME maps
#   ride the TENANT-lane `session` (recorded residual: labels, not money).
#   No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). Scoped membership is
#   ATTRIBUTION, not authorization — a channel moved between org units or
#   dropped from a group roster mid-request is never ranked under its former
#   scope (_snapshot_selection_channel_ids), and the deny-only grant
#   re-check covers org-unit and channel targets. Source ValidationErrors
#   propagate untouched for the route's 422 translation.
# Blast Radius: Every ranked number the rankings endpoint serves. Read-only —
#   the route owns the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/rankings.py -> the pure
#     roll-up builder.
#   - File: backend/ums_smart_revenue/finance/net_revenue.py -> the
#     per-channel summary this roll-up consumes.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_rankings(
    *,
    month: str,
    user: UserPrincipal,
    target_scope: AccessScope,
    channel_ids: set[str] | None,
    metric: str,
    limit: int,
    session: Session,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    override_repository: SqlAlchemyManualOverrideRepository,
    deduction_component_repository: SqlAlchemyDeductionComponentRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    committed_repository: SqlAlchemyCommittedAllocationRepository,
) -> tuple[MonthRankingsSummary, AllocationProvenance]:
    """Begin the composed-read snapshot, fetch the money sources, build rankings.

    Returns ``(rankings, allocation_provenance)``.
    """
    # FIX: One MVCC snapshot for every platform-lane money read below — facts,
    # overrides, deduction components, the committed-allocation read or its
    # live-compute fallback, and the org attribution maps that select AND
    # group the roll-up — so a writer or an org move committing mid-read can
    # no longer tear the ranked totals (REPEATABLE READ on Postgres;
    # db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    snapshot_org_index = load_org_access_index_from_session(platform_session)
    # Deny-only: a sector-granted caller whose target unit was reparented out
    # of the grant — or whose target channel was moved out of the granted
    # unit — between the gate and the snapshot must not receive snapshot-era
    # finance data under gate-era containment.
    _require_snapshot_org_scope_access(
        user,
        permissions=(Permission.VIEW_REVENUE, Permission.VIEW_CONFIDENCE),
        target_scope=target_scope,
        org_index=snapshot_org_index,
    )
    selection_channel_ids = _snapshot_selection_channel_ids(
        user,
        permissions=(Permission.VIEW_REVENUE, Permission.VIEW_CONFIDENCE),
        target_scope=target_scope,
        authorized_channel_ids=channel_ids,
        platform_session=platform_session,
        org_index=snapshot_org_index,
    )
    facts = revenue_repository.list_month_facts(
        month=month,
        youtube_channel_ids=selection_channel_ids,
    )
    overrides = override_repository.list_month_overrides(
        month=month,
        youtube_channel_ids=selection_channel_ids,
    )
    deduction_components = deduction_component_repository.list_month_components(
        month=month,
        youtube_channel_ids=selection_channel_ids,
        component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
    )
    # FIX: the close probe inside the resolver reads through the SNAPSHOT
    # session (see _load_month_net_revenue) — close status, run selection,
    # and live inputs all come from one MVCC snapshot.
    account_result, allocation_provenance = resolve_month_account_allocation(
        month=month,
        session=platform_session,
        deduction_repository=deduction_component_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        committed_repository=committed_repository,
    )
    # Scope-leak guard: account allocation resolves month-wide, so drop lines
    # for channels outside the resolved selection before they reach the
    # summary builder. selection_channel_ids is None for global reads.
    scoped_account_lines = filter_account_allocations_to_scope(
        account_result.lines, selection_channel_ids
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
    return (
        build_month_rankings(
            summary=summary,
            channel_company=snapshot_org_index.channel_company,
            channel_sector=snapshot_org_index.channel_sector,
            company_names=company_names,
            sector_names=sector_names,
            channel_names=channel_names,
            metric=metric,
            limit=limit,
        ),
        allocation_provenance,
    )


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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
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
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
    normalized_scope_type = target_scope.type.value
    normalized_scope_id = target_scope.id or "global"
    try:
        rankings, allocation_provenance = _load_month_rankings(
            month=month,
            user=user,
            target_scope=target_scope,
            channel_ids=channel_ids,
            metric=metric,
            limit=limit,
            session=session,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            override_repository=override_repository,
            deduction_component_repository=deduction_component_repository,
            link_repository=link_repository,
            committed_repository=committed_repository,
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
#   (resolve_smart_alert_tenant_id -> get_current_tenant, UMS_TENANT_ID
#   fallback); never hardcoded. Read surface only — no write/auth/audit.
# Blast Radius: Finance read display only; names never feed totals or ordering.
# Connections:
#   - File: backend/ums_smart_revenue/finance/rankings.py -> build_month_rankings
#       consumes channel_names for RankedEntry.entity_name on channel rows.
# ============================================================================
def _channel_name_map(session: Session) -> dict[str, str]:
    """Return active channel id->name for the current tenant (raw-id fallback)."""
    tenant_id = resolve_smart_alert_tenant_id()
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
    require_permission(user, Permission.MANAGE_BANK_RECONCILIATION, scope)
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


# ============================================================================
# Purpose: Data-access + composition step for the monthly bank
#   reconciliation, extracted out of the route handler (thin-orchestration
#   rule): begin the composed-read snapshot, fetch both sources once, and
#   build the summary.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads via the AdSensePayment and
#   BankReconciliation repositories. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gates always run first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). Source ValidationErrors
#   propagate untouched for the route's 422 translation.
# Blast Radius: Every number the bank-reconciliation endpoint serves.
#   Read-only — the route owns the audit events.
# Connections:
#   - File: backend/ums_smart_revenue/finance/bank_reconciliation.py -> the
#     pure summary builder and fee/FX evidence sums.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_month_bank_reconciliation(
    *,
    month: str,
    platform_session: Session,
    payment_repository: SqlAlchemyAdSensePaymentRepository,
    bank_repository: SqlAlchemyBankReconciliationRepository,
) -> MonthBankReconciliationSummary:
    """Begin the composed-read snapshot, fetch both sources, build the summary."""
    # FIX: One MVCC snapshot for both source reads below — a payment or bank
    # entry committing between them can no longer tear the composed summary
    # (REPEATABLE READ on Postgres; db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    payments = payment_repository.list_month_payments(month=month)
    entries = bank_repository.list_month_entries(month=month)
    return build_month_bank_reconciliation_summary(
        month=month,
        payments=payments,
        bank_entries=entries,
    )


# ============================================================================
# Purpose: Serve the monthly bank-reconciliation summary — AdSense payments
#   compared against recorded bank receipts (fee/FX evidence included) for one
#   month, with the match status and self-audit trail. Read-only; never
#   mutates finance numbers.
# Database/ORM: Reads via the AdSensePayment and BankReconciliation
#   repositories on the platform-lane session; appends
#   BANK_RECONCILIATION_VIEWED + PAYMENT_VIEWED audit events through the
#   revenue audit sink. No locks or writes to finance rows.
# Standards: Two-permission gate — VIEW_BANK_RECONCILIATION plus
#   VIEW_FINALIZED_PAYMENTS, both at finance-month scope; denial precedes any
#   source read. Both source reads happen inside one REPEATABLE READ
#   composed-read snapshot on Postgres (db/read_snapshot.py) begun by
#   _load_month_bank_reconciliation after the gates, so a payment or bank
#   entry committing mid-read cannot tear the composed summary; the handler
#   itself never touches the session (thin-orchestration rule).
# Blast Radius: Finance dashboard read surface; the bank-reconciliation wire
#   contract feeds the Command Center and the smart-alerts builder reuses the
#   same summary shape.
# Connections:
#   - File: backend/ums_smart_revenue/finance/bank_reconciliation.py -> the
#     pure summary builder and fee/FX evidence sums.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gates and the source reads.
#   - File: Docs/12_BACKEND_API_SPEC.md -> bank-reconciliation wire contract.
# ============================================================================
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return the bank-reconciliation summary for a month."""
    scope = AccessScope.finance_month(month)
    require_permission(user, Permission.VIEW_BANK_RECONCILIATION, scope)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        summary = _load_month_bank_reconciliation(
            month=month,
            platform_session=platform_session,
            payment_repository=payment_repository,
            bank_repository=bank_repository,
        )
    except (AdSensePaymentValidationError, BankReconciliationValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

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


# One audit entity-type literal for the composed gap-explanation read — the
# three view events must never drift apart on it.
_MONTH_GAP_EXPLANATION_ENTITY_TYPE = "month_gap_explanation"


# ============================================================================
# Purpose: Data-access + composition step for the month gap explanation,
#   extracted out of the route handler (thin-orchestration rule): normalize
#   the currency, fetch each source exactly once (facts, payments, bank
#   entries, close status), build both source summaries, and compose the
#   explanation.
# Database/ORM: Read-only via the four injected repositories — the same
#   repositories the payment-match, bank-reconciliation, and smart-alerts
#   endpoints already read; no writes, no new queries beyond their list/get
#   methods.
# Standards: One fetch feeds every builder (no double reads, no drift
#   between the legs' inputs); source ValidationErrors propagate untouched
#   for the route's 422 translation; close_status defaults to OPEN when no
#   close row exists. The close status is read BEFORE and AFTER the source
#   fetches: without a snapshot a close can commit mid-read, and labeling
#   pre-lock totals "LOCKED" would misstate a frozen month — on a detected
#   transition the sources are refetched ONCE (post-lock sources are frozen
#   by the locked-month write guards, so the retry pairs consistently). On
#   Postgres this loader begins the REPEATABLE READ composed-read snapshot
#   itself before its first fetch (db/read_snapshot.py) — NOT in a route
#   dependency, so the route's permission gates always run first (denial
#   precedes any transaction begin, pinned by the direct-call tests'
#   fail-if-touched platform-session stubs) — so every fetch here shares one
#   MVCC snapshot and no transition can be observed mid-read; the
#   detect-and-retry remains as the guard for non-Postgres dialects.
# Blast Radius: Every number the gap-explanation endpoint serves. Read-only
#   — no audit, no mutation (the route owns the audit triple).
# Connections:
#   - File: backend/ums_smart_revenue/finance/gap_explanation.py -> the
#     composition this loader feeds.
#   - File: tests/api/test_gap_explanation_api.py -> gate tests prove denial
#     precedes this loader (fail-if-touched repository stubs).
# ============================================================================
def _load_month_gap_explanation(
    *,
    month: str,
    currency: str,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    payment_repository: SqlAlchemyAdSensePaymentRepository,
    bank_repository: SqlAlchemyBankReconciliationRepository,
    close_repository: SqlAlchemyFinanceMonthCloseRepository,
) -> MonthGapExplanation:
    """Begin the composed-read snapshot, fetch the sources once, compose the explanation."""
    # FIX: One MVCC snapshot for every source read below — on Postgres a close
    # transition can no longer be observed mid-read, so the detect-and-retry
    # loop becomes the non-Postgres tier's guard (REPEATABLE READ;
    # db/read_snapshot.py holds the ruling).
    begin_composed_read_snapshot(platform_session)
    normalized_currency = normalize_payment_match_currency(currency)
    close = close_repository.get(month)
    close_status = close.status if close else "OPEN"
    for _ in range(2):
        facts = revenue_repository.list_month_facts(month=month)
        payments = payment_repository.list_month_payments(month=month)
        bank_entries = bank_repository.list_month_entries(month=month)
        close_after = close_repository.get(month)
        close_after_status = close_after.status if close_after else "OPEN"
        if close_after_status == close_status:
            break
        # A close transition committed mid-read: refetch the sources once so
        # the reported close state pairs with the totals it actually froze.
        close_status = close_after_status
    payment_summary = build_monthly_payment_match_summary(
        month=month,
        facts=facts,
        payments=payments,
        currency=normalized_currency,
    )
    bank_summary = build_month_bank_reconciliation_summary(
        month=month,
        payments=payments,
        bank_entries=bank_entries,
    )
    return build_month_gap_explanation(
        month=month,
        payment_summary=payment_summary,
        bank_summary=bank_summary,
        payments=payments,
        close_status=close_status,
    )


# ============================================================================
# Purpose: Serve the composed month gap explanation (Hard Problem #3) — both
#   legs of youtube_facts -> adsense_paid -> bank_received decomposed as
#   gap = evidence-backed components + unexplained residual, with provenance,
#   explain-shape confidence, and deterministic prose.
# Database/ORM: Read-only over the same repositories the payment-match and
#   bank-reconciliation endpoints already use, plus the month-close read the
#   smart-alerts endpoint models; one fetch feeds every builder. No writes.
# Standards: Permissions are the UNION of both source reads PLUS the global
#   confidence gate (VIEW_REVENUE + VIEW_CONFIDENCE @ global,
#   VIEW_FINALIZED_PAYMENTS + VIEW_BANK_RECONCILIATION @ finance_month — the
#   exact smart-alerts gate set): this response discloses every number both
#   sources disclose AND confidence labels/scores on every component and
#   residual, so it must not be readable with less. USD-only
#   via the shared normalizer; month-grain only; no FX conversion. Triple
#   audit (REVENUE_VIEWED + PAYMENT_VIEWED + BANK_RECONCILIATION_VIEWED),
#   the smart-alerts precedent, because all three surfaces' numbers appear;
#   the three appends land inside ONE AuditSink.transaction() boundary so no
#   tier can retain a partial triple for a failed response. The wire shape is
#   validated by MonthGapExplanationResponse (field order = wire order).
# Blast Radius: New read-only finance surface; payment-match and
#   bank-reconciliation payloads unchanged. No allocation, no net math,
#   no month-close writes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/gap_explanation.py -> the
#     builder and the ruled design pointer.
#   - File: Docs/12_BACKEND_API_SPEC.md -> the endpoint contract.
#   - File: frontend/src/lib/api/useGapExplanation.ts -> the Command Center
#     consumer.
# ============================================================================
@router.get("/months/{month}/gap-explanation", response_model=MonthGapExplanationResponse)
def get_month_gap_explanation(
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
    close_repository: Annotated[
        SqlAlchemyFinanceMonthCloseRepository,
        Depends(current_finance_month_close_repository),
    ],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Explain the month's payment and bank gaps as one composed narrative."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    require_permission(user, Permission.VIEW_REVENUE, global_scope)
    # Confidence labels, scores, and provenance-confidence tokens appear on
    # every component and residual, so the platform's confidence gate applies
    # exactly as it does on smart-alerts (the identical four-gate set).
    require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        explanation = _load_month_gap_explanation(
            month=month,
            currency=currency,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            payment_repository=payment_repository,
            bank_repository=bank_repository,
            close_repository=close_repository,
        )
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    explanation_api = explanation.to_api()
    # The three records disclose ONE composed read, so they land atomically:
    # if a later append fails, the sink's transaction boundary retracts the
    # accepted prefix instead of leaving a partial audit triple describing a
    # response that was never returned.
    with audit_sink.transaction():
        revenue_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type=_MONTH_GAP_EXPLANATION_ENTITY_TYPE,
            entity_id=month,
            scope=global_scope,
            details={
                "status": explanation.status,
                "payment_leg_status": explanation.payment_leg.status,
            },
        )
        payment_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type=_MONTH_GAP_EXPLANATION_ENTITY_TYPE,
            entity_id=month,
            scope=month_scope,
            details={
                "status": explanation.status,
                "payment_match_status": explanation.payment_leg.source_status,
            },
        )
        bank_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
            entity_type=_MONTH_GAP_EXPLANATION_ENTITY_TYPE,
            entity_id=month,
            scope=month_scope,
            details={
                "status": explanation.status,
                "bank_reconciliation_status": explanation.bank_leg.source_status,
            },
        )
    explanation_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
        audit_record_to_api(bank_record),
    ]
    return explanation_api


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
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
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
        require_permission(
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
    require_permission(user, Permission.CREATE_MANUAL_OVERRIDE, target_scope, org_index)
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
        raise_missing_permission(Permission.APPROVE_MANUAL_OVERRIDE)

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


# ============================================================================
# Purpose: Data-access step for the per-channel adjusted-revenue summary,
#   extracted out of the route handler (thin-orchestration rule): begin the
#   composed-read snapshot and fetch the channel's facts and overrides.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py), then reads via the RevenueFact and
#   ManualOverride repositories. No writes.
# Standards: The snapshot begins here — NOT in a route dependency — so the
#   route's permission gate always runs first: denial must precede any
#   transaction begin (pinned by the gap-explanation direct-call tests'
#   fail-if-touched platform-session stubs). The channel target is then
#   re-checked deny-only against the snapshot index
#   (_require_snapshot_org_scope_access): a caller admitted through an
#   inherited sector/company grant whose channel moved out of the granting
#   unit mid-request 403s instead of receiving snapshot-era sources under
#   gate-era containment; direct channel grants keep passing on scope
#   identity. Not-found and ValidationErrors propagate untouched for the
#   route's 404/422 translation; the summary is built by the route AFTER
#   that translation, exactly as before the extraction.
# Blast Radius: The adjusted summary's source coherence and stale-containment
#   refusal. Read-only — the route owns the build and the audit event.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_summary.py -> the
#     builder the route feeds with this pair.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first fetch.
# ============================================================================
def _load_channel_month_summary_sources(
    *,
    month: str,
    channel_id: str,
    user: UserPrincipal,
    platform_session: Session,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    override_repository: SqlAlchemyManualOverrideRepository,
) -> tuple[list[RevenueFactEntry], list[RevenueManualOverrideEntry]]:
    """Begin the composed-read snapshot, re-check the channel, fetch its sources."""
    # FIX: One MVCC snapshot for both source reads below — an override approval
    # or a fact write committing between them can no longer tear the adjusted
    # summary (REPEATABLE READ on Postgres; db/read_snapshot.py holds the
    # ruling).
    begin_composed_read_snapshot(platform_session)
    # Deny-only: a caller admitted via an inherited org grant must not be
    # served after the channel moved out of the granting unit mid-request.
    _require_snapshot_org_scope_access(
        user,
        permissions=(Permission.VIEW_REVENUE,),
        target_scope=AccessScope.channel(channel_id),
        org_index=load_org_access_index_from_session(platform_session),
    )
    facts = revenue_repository.list_channel_month_facts(
        month=month,
        youtube_channel_id=channel_id,
    )
    overrides = override_repository.list_channel_month_overrides(
        month=month,
        youtube_channel_id=channel_id,
    )
    return facts, overrides


# ============================================================================
# Purpose: Serve the per-channel adjusted-revenue summary for one month —
#   stored facts combined with approved manual overrides into the adjusted
#   number the dashboards display. Read-only; never mutates finance numbers.
# Database/ORM: Facts + overrides reads via the RevenueFact and ManualOverride
#   repositories on the platform-lane session; appends a REVENUE_VIEWED audit
#   event through the revenue audit sink. No locks or writes to finance rows.
# Standards: VIEW_REVENUE at channel scope through the org-access index;
#   denial precedes any source read. Both source reads happen inside one
#   REPEATABLE READ composed-read snapshot on Postgres (db/read_snapshot.py)
#   begun by _load_channel_month_summary_sources after the gate, so an
#   override approval or fact write committing mid-read cannot tear the
#   adjusted summary; the handler itself never touches the session
#   (thin-orchestration rule).
# Blast Radius: Channel-level finance read surface; 404 on unknown
#   channel/month, 422 on malformed month or override validation.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_summary.py -> the
#     adjusted-summary builder.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the composed-read
#     snapshot begun between the gate and the source reads.
#   - File: Docs/12_BACKEND_API_SPEC.md -> channel-month summary contract.
# ============================================================================
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
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return the adjusted-revenue summary for a channel and month, including manual overrides."""
    target_scope = AccessScope.channel(channel_id)
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts, overrides = _load_channel_month_summary_sources(
            month=month,
            channel_id=channel_id,
            user=user,
            platform_session=platform_session,
            revenue_repository=revenue_repository,
            override_repository=override_repository,
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
        require_permission(user, permission, target_scope, org_index)
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


# ============================================================================
# Purpose: Assemble the audit-derived smart-alert inputs for the monthly
#   dashboard route around the caller-provided coverage pair. Connector audit
#   signals are only read when the caller already passed the VIEW_AUDIT_LOG
#   gate.
# Database/ORM: Optional AuditLogORM and audit-log repository connector-run
#   scans on the tenant-lane session. No locks or writes. The missing-facts
#   coverage pair is NOT read here: it is not audit-gated, so the route reads
#   it inside the platform-lane composed-read snapshot (with the facts it is
#   compared against) and passes the values in — a fact committing mid-read
#   must not flip the coverage alert against money alerts built from the
#   pre-commit snapshot.
# Standards: Keeps the route from carrying branch-local audit counters across
#   permission paths; sensitive skipped-row reasons remain controlled by
#   VIEW_SENSITIVE_AUDIT_PAYLOADS.
# Blast Radius: Finance dashboard read surface and audit-observability boundary.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> dataclass
#     consumed by the pure alert builder.
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> failed
#     connector-run audit status aggregation used here.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot the
#     coverage pair is read inside of, at the route.
# ============================================================================
def _month_smart_alert_audit_signals(
    session: Session,
    *,
    month: str,
    missing_fact_channel_count: int,
    missing_fact_channel_sample: list[str],
    can_view_audit_log: bool,
    include_sensitive_details: bool,
) -> MonthlySmartAlertAuditSignals:
    """Return permission-gated audit signals around the snapshot-read coverage pair."""
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
    failed_connector_runs = SqlAlchemyAuditLogRepository(
        session,
        tenant_id=resolve_smart_alert_tenant_id(),
    ).connector_run_failure_summary(
        month=month,
    )
    return MonthlySmartAlertAuditSignals(
        missing_revenue_fact_channel_count=missing_fact_channel_count,
        missing_revenue_fact_channel_sample=missing_fact_channel_sample,
        skipped_source_row_count=skipped_source_row_count,
        skipped_source_rows_by_reason=skipped_source_rows_by_reason,
        failed_connector_run_count=failed_connector_runs.count,
        failed_connector_runs_by_status=failed_connector_runs.by_status,
    )


# ============================================================================
# Purpose: The scope types whose grant coverage can drift with org edits
#   between the tenant-lane permission gates and the composed-read snapshot —
#   org units (a target unit reparented out of the granting sector) and
#   channels (a target channel moved between units under an inherited
#   grant). This tuple decides which composed reads receive the fail-closed
#   _require_snapshot_org_scope_access re-check.
# Database/ORM: None; a pure ScopeType constant consumed by the re-check
#   helper's gate.
# Standards: Deliberately EXCLUDES GLOBAL (containment cannot drift with org
#   edits) and GROUP (authorization is per-member at gate time; the surviving
#   roster is permission-filtered member-by-member inside
#   _snapshot_selection_channel_ids instead, and the group's own active flag
#   re-reads there). Adding a type here can only ADD deny-only re-checks;
#   removing one silently revives the stale-containment epoch mix the
#   reparent/channel-move pins prove dead.
# Blast Radius: Which scoped composed reads can 403 on snapshot-state drift.
#   No grant is ever widened by this tuple.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py ->
#     _require_snapshot_org_scope_access gates on membership here; the
#     net-revenue/rankings/dry-run-recalculation loaders use it to decide
#     when to load the snapshot index.
#   - File: tests/api/test_composed_read_snapshot_postgres.py -> the
#     reparented-target and moved-channel pins that turn red if a type is
#     dropped.
# ============================================================================
_SNAPSHOT_GRANT_RECHECK_SCOPES = (ScopeType.SECTOR, ScopeType.COMPANY, ScopeType.CHANNEL)


# ============================================================================
# Purpose: Fail-closed re-check of an org-unit or channel scope's grant
#   coverage against the SNAPSHOT org-access index — deny-only, run by
#   composed-read loaders after they load the snapshot index.
# Database/ORM: None; pure has_permission evaluation over the caller's grants
#   and the passed (snapshot) index. The index itself was read inside the
#   caller's composed-read snapshot.
# Standards: DENY-ONLY by design: the tenant-lane permission gates already
#   admitted the request before any snapshot began (that laning is the
#   authorization boundary and platform-lane data must never GRANT access);
#   this re-check can only narrow — a sector-granted caller whose target
#   company was reparented out of the sector, or whose target CHANNEL was
#   moved out of the granted unit, between the gate and the snapshot is
#   refused, so the response never serves snapshot-era finance data under
#   gate-era containment. Direct channel grants keep passing on scope
#   identity alone (has_permission needs no index for a same-scope match);
#   only inherited sector/company containment re-evaluates. The 403 carries
#   the same missing-permission message as the gate, keeping unauthorized
#   probes and unauthorized reads indistinguishable. Global targets cannot
#   drift with org edits, and GROUP targets are not re-checked here as a
#   unit: their authorization is per-member, so the surviving roster is
#   permission-filtered member-by-member (and the group's active flag
#   re-read) inside _snapshot_selection_channel_ids.
# Blast Radius: Scoped composed reads only; can only turn a would-have-served
#   response into a 403, never the reverse.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission, the
#     same evaluation the gates ran on the tenant index.
#   - File: backend/ums_smart_revenue/org/access_index.py -> the snapshot
#     index this re-check evaluates containment against.
# ============================================================================
def _require_snapshot_org_scope_access(
    user: UserPrincipal,
    *,
    permissions: tuple[Permission, ...],
    target_scope: AccessScope,
    org_index: OrgAccessIndex,
) -> None:
    """Re-assert org-unit/channel scope coverage on the snapshot index (deny-only)."""
    if target_scope.type not in _SNAPSHOT_GRANT_RECHECK_SCOPES:
        return
    for permission in permissions:
        require_permission(user, permission, target_scope, org_index)


# ============================================================================
# Purpose: Re-resolve the org-derived SELECTION channel set for a scoped money
#   read from a given org-access index — the attribution half of scope
#   resolution, split from authorization so composed-read loaders can rerun it
#   on the snapshot index.
# Database/ORM: None; pure derivation over the passed index (mirrors
#   resolve_revenue_read_scope's sector/company comprehensions exactly).
# Standards: Company/sector membership is ATTRIBUTION — it decides whose money
#   rolls into the unit's totals — so it must come from the same MVCC snapshot
#   as the money rows (a channel moved between units mid-request must never be
#   served under its former unit). Selection is the INTERSECTION of snapshot
#   membership with the gate-time authorized set: authorization ran at
#   org-unit grain on the TENANT-lane index before any snapshot began, and a
#   channel must belong to the unit in BOTH states to be served — a channel
#   moved in mid-request is excluded (under a sector grant whose company was
#   reparented it would be authorized in neither state), and a channel moved
#   out is excluded by the snapshot side. This function is INDEX-ONLY: GROUP
#   membership needs the registry, so its snapshot-side re-read lives in
#   _snapshot_selection_channel_ids, which delegates the org-unit branches
#   here. Channel scope is a literal id; global is None. An empty result
#   returns an empty set (empty summary, not 403) — the same contract as the
#   tenant-side resolver.
# Blast Radius: Which channels' money feeds scoped net-revenue/rankings
#   responses. No authorization decision is made here.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_scopes.py -> the
#     tenant-side resolver whose sector/company selection this mirrors.
#   - File: backend/ums_smart_revenue/org/access_index.py -> the index loader
#     the composed-read loaders call on the snapshot session.
# ============================================================================
def _org_scope_member_channel_ids(
    *,
    target_scope: AccessScope,
    authorized_channel_ids: set[str] | None,
    org_index: OrgAccessIndex,
) -> set[str] | None:
    """Derive the selection channel set for a scope from the given org index.

    Org-unit scopes intersect snapshot membership with the gate-time
    authorized set: a channel is served only when it belongs to the unit in
    BOTH database states, so a channel moved in mid-request (authorized in
    neither the gate-time nor a grant-relevant snapshot state — e.g. under a
    sector grant whose company was reparented) is never selected, and a
    channel moved out is dropped by the snapshot side.
    """
    if target_scope.type == ScopeType.SECTOR:
        members = {
            channel_id
            for channel_id, sector_id in org_index.channel_sector.items()
            if sector_id == target_scope.id
        }
    elif target_scope.type == ScopeType.COMPANY:
        members = {
            channel_id
            for channel_id, company_id in org_index.channel_company.items()
            if company_id == target_scope.id
        }
    else:
        return authorized_channel_ids
    if authorized_channel_ids is None:
        return members
    return members & authorized_channel_ids


# ============================================================================
# Purpose: Re-resolve the SELECTION channel set for a scoped composed read on
#   the caller's snapshot session — one entry point covering every scope
#   type, so the loaders derive attribution from the same MVCC snapshot as
#   the money rows it selects.
# Database/ORM: Org-unit scopes derive from the passed snapshot index
#   (loading it from the snapshot session if the caller had no other need for
#   it); GROUP scope re-reads the group row and its active membership via
#   SqlAlchemyChannelGroupRegistry on the snapshot session. Read-only.
# Standards: DENY-ONLY throughout: every branch INTERSECTS snapshot
#   membership with the gate-time authorized set, so platform-lane data can
#   only narrow the selection, never admit a channel the tenant-lane gates
#   did not cover — org-unit branches via _org_scope_member_channel_ids
#   (both-states rule), GROUP via the registry's active-member re-read (a
#   member dropped from the roster mid-request stops feeding the rollup; a
#   member added mid-request was never per-member authorized and stays out)
#   FOLLOWED by a per-member grant filter on the snapshot index: group
#   authorization is per-member, so a surviving member whose inherited
#   sector/company containment drifted between the gate and the snapshot is
#   dropped too, while direct channel grants keep passing on scope identity
#   alone. The group's own ACTIVE flag also re-reads on the snapshot: a
#   group archived or deleted mid-request yields an empty selection (empty
#   summary, not 403 — the resolver's 403 is a GATE decision and already
#   ran). Channel scope passes the literal gate-time set through (its
#   containment is re-checked by _require_snapshot_org_scope_access, not
#   narrowed here); global is None.
# Blast Radius: Which channels' money feeds scoped net-revenue, rankings,
#   and dry-run recalculation responses. Deny-only — no channel is ever
#   added, and the group member filter can only remove.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
#     get_group + get_active_member_channels, the same reads the gate-time
#     resolver ran on the tenant lane.
#   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission, the
#     per-member evaluation mirroring the gate's covered-subset check.
#   - File: backend/ums_smart_revenue/finance/revenue_scopes.py -> the
#     tenant-side resolver whose selection semantics every branch mirrors.
# ============================================================================
def _snapshot_selection_channel_ids(
    user: UserPrincipal,
    *,
    permissions: tuple[Permission, ...],
    target_scope: AccessScope,
    authorized_channel_ids: set[str] | None,
    platform_session: Session,
    org_index: OrgAccessIndex | None,
) -> set[str] | None:
    """Derive the scoped selection set on the snapshot session (deny-only).

    ``org_index`` is the snapshot org-access index when the caller already
    loaded one for the grant re-check; branches needing it load it from the
    snapshot session otherwise. ``permissions`` are the route's read gates,
    re-evaluated per surviving group member.
    """
    if target_scope.type in (ScopeType.SECTOR, ScopeType.COMPANY):
        index = org_index
        if index is None:
            index = load_org_access_index_from_session(platform_session)
        return _org_scope_member_channel_ids(
            target_scope=target_scope,
            authorized_channel_ids=authorized_channel_ids,
            org_index=index,
        )
    if target_scope.type == ScopeType.GROUP:
        registry = SqlAlchemyChannelGroupRegistry(platform_session)
        group = registry.get_group(target_scope.id or "")
        if group is None or not group.active:
            return set()
        snapshot_members = set(
            registry.get_active_member_channels(target_scope.id or "") or ()
        )
        if authorized_channel_ids is not None:
            snapshot_members &= authorized_channel_ids
        if not snapshot_members:
            return snapshot_members
        index = org_index
        if index is None:
            index = load_org_access_index_from_session(platform_session)
        return {
            member
            for member in snapshot_members
            if all(
                has_permission(user, permission, AccessScope.channel(member), index)
                for permission in permissions
            )
        }
    return authorized_channel_ids


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
