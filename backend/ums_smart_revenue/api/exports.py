# ============================================================================
# Purpose: Expose export request, listing, preview, and artifact-download routes
# while keeping export authorization, artifact persistence, and audit writes
# centralized behind typed helpers.
# Database/ORM: ExportJobORM plus finance/source-row reads for generated files.
# Standards: Thin FastAPI routes, fail-closed permissions, safe errors, and
# typed audit/artifact boundaries.
# Blast Radius: Authorization, finance exports, analytics CSV exports, audit
# logs, and export artifact metadata.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Export job storage.
#   - File: backend/ums_smart_revenue/reports/artifact_storage.py -> Artifact IO.
# ============================================================================
import logging
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.authz import raise_missing_permission, require_permission
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.dependencies_audit import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies_finance import current_org_access_index
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_log import SqlAlchemyAuditLogRepository
from ums_smart_revenue.auth.audit_service import AuditRecord, AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.db.read_snapshot import begin_composed_read_snapshot
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.allocation import AllocationValidationError
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationValidationError,
    MonthBankReconciliationSummary,
    SqlAlchemyBankReconciliationRepository,
    build_month_bank_reconciliation_summary,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.deduction_policy import NET_APPLICABLE_COMPONENT_KINDS
from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideValidationError,
    SqlAlchemyManualOverrideRepository,
)
from ums_smart_revenue.finance.month_close import SqlAlchemyFinanceMonthCloseRepository
from ums_smart_revenue.finance.net_revenue import (
    MonthNetRevenueSummary,
    NetRevenueValidationError,
    build_month_net_revenue_summary,
    filter_account_allocations_to_scope,
)
from ums_smart_revenue.finance.payment_matching import (
    MonthlyPaymentMatchSummary,
    PaymentMatchValidationError,
    build_monthly_payment_match_summary,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
)
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
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.reports.analytics_summary_csv import (
    AnalyticsSummaryCsvValidationError,
    build_analytics_summary_csv,
)
from ums_smart_revenue.reports.artifact_storage import (
    ExportArtifactStorageError,
    FileSystemExportArtifactStore,
)
from ums_smart_revenue.reports.branded_slide_pack import (
    BrandedSlidePackValidationError,
    build_branded_slide_pack_pptx,
    build_branded_slide_pack_report,
)
from ums_smart_revenue.reports.executive_pdf import (
    ExecutivePdfValidationError,
    build_executive_pdf_bytes,
    build_executive_pdf_report,
)
from ums_smart_revenue.reports.exports import (
    ALLOWED_EXPORT_TYPES,
    MAX_EXPORT_JOB_PAGE_SIZE,
    ExportJobEntry,
    ExportJobNotFoundError,
    ExportJobTerminalStateError,
    ExportJobValidationError,
    SqlAlchemyExportJobRepository,
    is_finance_export_type,
)
from ums_smart_revenue.reports.finance_workbook import (
    FinanceWorkbookPreview,
    FinanceWorkbookPreviewValidationError,
    build_finance_workbook_preview,
    build_finance_workbook_xlsx,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

router = APIRouter(prefix="/exports", tags=["exports"])
logger = logging.getLogger(__name__)
MAX_AUTHORIZED_EXPORT_JOB_SCAN_PAGES = 10
_ANALYTICS_SUMMARY_CSV_TYPE = "ANALYTICS_SUMMARY_CSV"
_ANALYTICS_SUMMARY_CSV_REQUIRED_PERMISSIONS = (
    Permission.EXPORT_ANALYTICS_REPORT,
    Permission.VIEW_ANALYTICS,
    Permission.VIEW_REVENUE,
)
_TERMINAL_EXPORT_JOB_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_CONNECTOR_SMART_ALERT_CODES = frozenset({"CONNECTOR_RUNS_FAILED", "SOURCE_ROWS_SKIPPED"})


@dataclass(frozen=True)
class _FinanceExportSourceSummaries:
    """Frozen bundle of finance source summaries resolved for a single export job."""

    net_revenue: MonthNetRevenueSummary
    payment_match: MonthlyPaymentMatchSummary
    bank_reconciliation: MonthBankReconciliationSummary
    smart_alerts: MonthlySmartAlertSummary
    account_allocation_provenance: AllocationProvenance


@dataclass(frozen=True)
class _ExportDownloadArtifact:
    """Persisted export bytes plus the job state that produced the response."""

    export_job: ExportJobEntry
    content: bytes
    filename: str
    content_type: str


@dataclass(frozen=True)
class _FinanceExportSourceContext:
    """Dependencies needed to build finance export source summaries.

    ``session`` is the tenant-lane request session (audit-gated signal reads
    only); ``platform_session`` carries every finance source read inside the
    composed-read snapshot the builder begins.
    """

    export_job: ExportJobEntry
    user: UserPrincipal
    session: Session
    platform_session: Session
    org_index: OrgAccessIndex
    group_registry: ChannelGroupRegistryStore
    include_audit_derived_alerts: bool = True


@dataclass(frozen=True)
class _FinanceExportAuditContext:
    """Dependencies shared by finance export artifact audit writers."""

    audit_sink: AuditSink
    user: UserPrincipal
    export_job: ExportJobEntry
    group_registry: ChannelGroupRegistryStore


class ExportRequest(BaseModel):
    """Pydantic request model for creating a new export job."""

    export_type: str = Field(min_length=1)
    template_id: str | None = None
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    month: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=1)
    include_confidence_notes: bool = True
    include_manual_override_notes: bool = True
    reason: str = Field(min_length=1)

    @field_validator("export_type", "scope_type", "month", "currency", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace and reject blank strings for required fields."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("scope_id", "template_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Strip whitespace from optional string fields and coerce blank to None."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_export_job_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyExportJobRepository:
    """FastAPI dependency that returns a scoped export job repository."""
    return SqlAlchemyExportJobRepository(session)


def current_export_artifact_store() -> FileSystemExportArtifactStore:
    """FastAPI dependency that returns the configured export artifact store."""
    return FileSystemExportArtifactStore.from_environment()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def request_export(
    payload: ExportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Accept a new export request, authorize the caller, snapshot scope, and enqueue the job."""
    try:
        # Validate export_type at the trust boundary so a typo returns 422
        # ("Unknown export_type") instead of falling through to the analytics
        # permission gate and producing a 403 for users with finance grants.
        if payload.export_type not in ALLOWED_EXPORT_TYPES:
            raise ExportJobValidationError(f"Unknown export_type: {payload.export_type}")
        required_export_permission = _audit_permission_for_export_type(payload.export_type)
        if not _has_permission_assignment(user, required_export_permission):
            raise_missing_permission(required_export_permission)
        analytics_csv_lookup_denial_permission: Permission | None = None
        if payload.export_type == _ANALYTICS_SUMMARY_CSV_TYPE:
            analytics_csv_lookup_denial_permission = (
                _require_analytics_csv_permissions_before_scope_lookup(
                    user=user,
                    scope_type=payload.scope_type,
                    scope_id=payload.scope_id,
                    org_index=org_index,
                )
            )
        # Pre-check channel-scope authorization before resolving the channel
        # snapshot so an unauthorized caller cannot probe channel existence
        # via 404 responses. Snapshot resolution can still raise 404 below,
        # but only after the caller has proven channel access.
        if payload.scope_type == "channel" and payload.scope_id:
            _require_export_access_permissions(
                user=user,
                export_type=payload.export_type,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                month=payload.month,
                org_index=org_index,
                group_registry=group_registry,
                scope_channel_ids=(payload.scope_id,),
            )
        snapshot_tuple = _resolve_export_scope_snapshot(
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            org_index=org_index,
            group_registry=group_registry,
            analytics_csv_lookup_denial_permission=analytics_csv_lookup_denial_permission,
        )
        _require_export_access_permissions(
            user=user,
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            month=payload.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=snapshot_tuple,
        )
        if payload.export_type == _ANALYTICS_SUMMARY_CSV_TYPE:
            # FIX: The CSV carries revenue amounts, so creation must match the
            # download gate and reject analytics-only users before a queued,
            # undownloadable export job is persisted.
            _require_export_scope_view_permission(
                user=user,
                view_permission=Permission.VIEW_REVENUE,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                org_index=org_index,
                group_registry=group_registry,
                scope_channel_ids=snapshot_tuple,
            )
        export_job = repository.request_export(
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            month=payload.month,
            currency=payload.currency,
            actor_user_id=user.user_id,
            include_confidence_notes=payload.include_confidence_notes,
            include_manual_override_notes=payload.include_manual_override_notes,
            scope_channel_ids=snapshot_tuple,
            template_id=payload.template_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_CREATED,
        entity_type="export_job",
        entity_id=export_job.id,
        scope=AccessScope.export(export_job.id),
        reason=payload.reason,
        permission_override=_audit_permission_for_export_type(export_job.export_type),
        details={
            "export_type": export_job.export_type,
            "template_id": export_job.template_id,
            "scope_type": export_job.scope_type,
            "scope_id": export_job.scope_id,
            "month": export_job.month,
            "currency": export_job.currency,
            "month_lock_status": export_job.month_lock_status,
        },
    )
    response = export_job.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("")
def list_exports(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_JOB_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return a paginated list of export jobs the caller is authorized to access."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        items, has_more = _list_authorized_export_jobs(
            repository=repository,
            user=user,
            org_index=org_index,
            group_registry=group_registry,
            limit=limit,
            offset=offset,
        )
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return {
        "items": [item.to_api() for item in items],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(items),
            "has_more": has_more,
        },
    }


@router.get("/{export_id}")
def get_export(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Retrieve a single export job by ID and emit a scoped access audit event."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    try:
        _require_export_access_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc).strip("'"),
        ) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    details: dict[str, object] = {
        "export_type": export_job.export_type,
        "scope_type": export_job.scope_type,
        "scope_id": export_job.scope_id,
        "month": export_job.month,
        "status": export_job.status,
    }
    if export_job.file_url:
        details.update(_artifact_metadata_audit_details(export_job))
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_VIEWED,
        entity_type="export_job",
        entity_id=export_job.id,
        scope=AccessScope.export(export_job.id),
        permission_override=_audit_permission_for_export_type(export_job.export_type),
        details=details,
    )
    response = export_job.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("/{export_id}/finance-workbook-preview")
def preview_finance_workbook(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    session: Annotated[Session, Depends(current_db_session)],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Return a JSON preview of the finance workbook data for a FINANCE_EXCEL export job."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
        _require_finance_export_artifact_permissions(
            user=user,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
        if export_job.export_type != "FINANCE_EXCEL":
            raise FinanceWorkbookPreviewValidationError(
                "finance workbook preview only supports FINANCE_EXCEL exports"
            )
        preview = _build_finance_workbook_preview_for_export(
            context=_FinanceExportSourceContext(
                export_job=export_job,
                user=user,
                session=session,
                platform_session=platform_session,
                org_index=org_index,
                group_registry=group_registry,
            ),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        AllocationValidationError,
        BankReconciliationValidationError,
        DeductionComponentValidationError,
        ExportJobValidationError,
        FinanceWorkbookPreviewValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    audit_records = _record_finance_export_artifact_audit(
        context=_FinanceExportAuditContext(
            audit_sink=audit_sink,
            user=user,
            export_job=export_job,
            group_registry=group_registry,
        ),
        artifact_type="finance_workbook_preview",
        include_download_event=False,
        audit_summary=preview.smart_alerts if export_job.scope_type == "global" else None,
    )
    response = preview.to_api()
    response["audit_events"] = [audit_record_to_api(record) for record in audit_records]
    return response


# ============================================================================
# Purpose: Download a persisted or freshly-generated ANALYTICS_SUMMARY_CSV
# artifact for the requesting user after export-owner and scoped read checks.
# Database/ORM: ExportJobORM lookup and audit insert; generation helper reads
# google_revenue_source_rows/youtube_channels.
# Standards: Thin route, fail-closed analytics+revenue authorization, typed
# validation/storage errors, and audit after successful artifact availability.
# Blast Radius: Analytics export downloads, finance-visible source amounts,
# artifact checksums, and audit logs.
# Connections:
#   - File: backend/ums_smart_revenue/reports/analytics_summary_csv.py -> CSV builder.
#   - File: Docs/12_BACKEND_API_SPEC.md -> Route contract.
# ============================================================================
@router.get("/{export_id}/analytics-summary.csv")
def download_analytics_summary_csv(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    """Generate or serve the cached analytics summary CSV for an analytics export job."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
        resolved_channel_ids = _resolved_export_channel_ids(
            export_job=export_job,
            org_index=org_index,
            group_registry=group_registry,
        )
        _require_analytics_export_artifact_permissions(
            user=user,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=_channel_snapshot_tuple(resolved_channel_ids),
        )
        artifact = _load_analytics_summary_csv_artifact(
            repository=repository,
            export_job=export_job,
            artifact_store=artifact_store,
            session=session,
            tenant_id=_tenant_uuid(user),
            scope_channel_ids=resolved_channel_ids,
        )
        if isinstance(artifact, JSONResponse):
            return artifact
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (AnalyticsSummaryCsvValidationError, ExportJobValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    _record_analytics_export_artifact_audit(
        audit_sink=audit_sink,
        user=user,
        export_job=artifact.export_job,
        group_registry=group_registry,
        artifact_type="analytics_summary_csv",
    )
    return Response(
        content=artifact.content,
        media_type=artifact.content_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )


@router.get("/{export_id}/finance-workbook.xlsx")
def download_finance_workbook(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    """Generate or serve the cached finance workbook XLSX file for a FINANCE_EXCEL export job."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
        _require_finance_export_artifact_permissions(
            user=user,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
        if export_job.export_type != "FINANCE_EXCEL":
            raise FinanceWorkbookPreviewValidationError(
                "finance workbook download only supports FINANCE_EXCEL exports"
            )
        served = _serve_persisted_artifact_bytes(
            export_job=export_job,
            expected_export_type="FINANCE_EXCEL",
            artifact_store=artifact_store,
        )
        if served is not None:
            workbook_bytes, filename, content_type = served
        else:
            preview = _build_finance_workbook_preview_for_export(
                context=_FinanceExportSourceContext(
                    export_job=export_job,
                    user=user,
                    session=session,
                    platform_session=platform_session,
                    org_index=org_index,
                    group_registry=group_registry,
                    include_audit_derived_alerts=False,
                ),
            )
            workbook_bytes = build_finance_workbook_xlsx(preview)
            filename = f"ums-finance-{export_job.month}-{export_job.scope_type}.xlsx"
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            persisted_export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=workbook_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(persisted_export_job)
            workbook_bytes, filename, content_type = _require_persisted_artifact_bytes(
                export_job=export_job,
                expected_export_type="FINANCE_EXCEL",
                artifact_store=artifact_store,
            )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        AllocationValidationError,
        BankReconciliationValidationError,
        DeductionComponentValidationError,
        ExportJobValidationError,
        FinanceWorkbookPreviewValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    _record_finance_export_artifact_audit(
        context=_FinanceExportAuditContext(
            audit_sink=audit_sink,
            user=user,
            export_job=export_job,
            group_registry=group_registry,
        ),
        artifact_type="finance_workbook_xlsx",
        include_download_event=True,
    )
    return Response(
        content=workbook_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{export_id}/executive.pdf")
def download_executive_pdf(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    """Generate or serve the cached executive summary PDF for an EXECUTIVE_PDF export job."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
        _require_finance_export_artifact_permissions(
            user=user,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
        if export_job.export_type != "EXECUTIVE_PDF":
            raise ExecutivePdfValidationError(
                "executive PDF download only supports EXECUTIVE_PDF exports"
            )
        served = _serve_persisted_artifact_bytes(
            export_job=export_job,
            expected_export_type="EXECUTIVE_PDF",
            artifact_store=artifact_store,
        )
        if served is not None:
            pdf_bytes, filename, content_type = served
        else:
            source_summaries = _build_finance_source_summaries_for_export(
                context=_FinanceExportSourceContext(
                    export_job=export_job,
                    user=user,
                    session=session,
                    platform_session=platform_session,
                    org_index=org_index,
                    group_registry=group_registry,
                    include_audit_derived_alerts=False,
                ),
            )
            report = build_executive_pdf_report(
                export_job=export_job,
                net_revenue=source_summaries.net_revenue,
                payment_match=source_summaries.payment_match,
                bank_reconciliation=source_summaries.bank_reconciliation,
                smart_alerts=source_summaries.smart_alerts,
                account_allocation_provenance=(source_summaries.account_allocation_provenance),
            )
            pdf_bytes = build_executive_pdf_bytes(report)
            filename = f"ums-executive-{export_job.month}-{export_job.scope_type}.pdf"
            content_type = "application/pdf"
            persisted_export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=pdf_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(persisted_export_job)
            pdf_bytes, filename, content_type = _require_persisted_artifact_bytes(
                export_job=export_job,
                expected_export_type="EXECUTIVE_PDF",
                artifact_store=artifact_store,
            )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        AllocationValidationError,
        BankReconciliationValidationError,
        DeductionComponentValidationError,
        ExecutivePdfValidationError,
        ExportJobValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    _record_finance_export_artifact_audit(
        context=_FinanceExportAuditContext(
            audit_sink=audit_sink,
            user=user,
            export_job=export_job,
            group_registry=group_registry,
        ),
        artifact_type="executive_pdf",
        include_download_event=True,
    )
    return Response(
        content=pdf_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{export_id}/branded-slide-pack.pptx")
def download_branded_slide_pack(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    platform_session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    """Generate or serve the cached branded slide pack PPTX for a BRANDED_SLIDE_PACK export job."""
    if not _has_any_export_permission(user):
        raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
        _require_finance_export_artifact_permissions(
            user=user,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
        if export_job.export_type != "BRANDED_SLIDE_PACK":
            raise BrandedSlidePackValidationError(
                "branded slide pack download only supports BRANDED_SLIDE_PACK exports"
            )
        served = _serve_persisted_artifact_bytes(
            export_job=export_job,
            expected_export_type="BRANDED_SLIDE_PACK",
            artifact_store=artifact_store,
        )
        if served is not None:
            pptx_bytes, filename, content_type = served
        else:
            source_summaries = _build_finance_source_summaries_for_export(
                context=_FinanceExportSourceContext(
                    export_job=export_job,
                    user=user,
                    session=session,
                    platform_session=platform_session,
                    org_index=org_index,
                    group_registry=group_registry,
                    include_audit_derived_alerts=False,
                ),
            )
            report = build_branded_slide_pack_report(
                export_job=export_job,
                net_revenue=source_summaries.net_revenue,
                payment_match=source_summaries.payment_match,
                bank_reconciliation=source_summaries.bank_reconciliation,
                smart_alerts=source_summaries.smart_alerts,
                account_allocation_provenance=(source_summaries.account_allocation_provenance),
            )
            pptx_bytes = build_branded_slide_pack_pptx(report)
            filename = f"ums-branded-{export_job.month}-{export_job.scope_type}.pptx"
            content_type = (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
            persisted_export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=pptx_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(persisted_export_job)
            pptx_bytes, filename, content_type = _require_persisted_artifact_bytes(
                export_job=export_job,
                expected_export_type="BRANDED_SLIDE_PACK",
                artifact_store=artifact_store,
            )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        AllocationValidationError,
        BankReconciliationValidationError,
        BrandedSlidePackValidationError,
        DeductionComponentValidationError,
        ExportJobValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    _record_finance_export_artifact_audit(
        context=_FinanceExportAuditContext(
            audit_sink=audit_sink,
            user=user,
            export_job=export_job,
            group_registry=group_registry,
        ),
        artifact_type="branded_slide_pack_pptx",
        include_download_event=True,
    )
    return Response(
        content=pptx_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_finance_workbook_preview_for_export(
    *,
    context: _FinanceExportSourceContext,
) -> FinanceWorkbookPreview:
    """Build the finance workbook preview model for the given export job."""
    source_summaries = _build_finance_source_summaries_for_export(
        context=context,
    )
    return build_finance_workbook_preview(
        export_job=context.export_job,
        net_revenue=source_summaries.net_revenue,
        payment_match=source_summaries.payment_match,
        bank_reconciliation=source_summaries.bank_reconciliation,
        smart_alerts=source_summaries.smart_alerts,
        account_allocation_provenance=source_summaries.account_allocation_provenance,
    )


def _tenant_uuid(user: UserPrincipal) -> UUID:
    """Return the current tenant UUID, falling back to the UMS default tenant."""
    return UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)


# ============================================================================
# Purpose: Return a safe conflict response when a terminal export lacks a
# persisted artifact that can be served.
# Database/ORM: None.
# Standards: Fail closed before source-row reads or artifact writes.
# Blast Radius: Analytics CSV downloads, revenue source confidentiality, audit.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Terminal job model.
#   - File: tests/api/test_exports_api.py -> Terminal CSV download regression.
# ============================================================================
def _terminal_export_conflict_response(export_job: ExportJobEntry) -> JSONResponse:
    """Return a safe 409 response for terminal jobs without served artifacts."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": f"Export job is already in terminal status {export_job.status}"},
    )


# ============================================================================
# Purpose: Resolve the bytes, filename, content type, and persisted job metadata
# for an analytics CSV download, serving cached artifacts before generating.
# Database/ORM: ExportJobORM complete_artifact write; non-terminal source-row
# reads happen in build_analytics_summary_csv.
# Standards: Route stays thin; storage failures remain retryable 503 responses;
# completed artifact metadata is re-read before audit emission; terminal rows
# without artifacts are rejected before source-row reads.
# Blast Radius: Analytics CSV artifact bytes, checksum metadata, export status.
# Connections:
#   - File: backend/ums_smart_revenue/reports/analytics_summary_csv.py -> CSV builder.
#   - File: backend/ums_smart_revenue/reports/artifact_storage.py -> Persisted bytes.
# ============================================================================
def _load_analytics_summary_csv_artifact(
    *,
    repository: SqlAlchemyExportJobRepository,
    export_job: ExportJobEntry,
    artifact_store: FileSystemExportArtifactStore,
    session: Session,
    tenant_id: UUID,
    scope_channel_ids: set[str] | None,
) -> _ExportDownloadArtifact | JSONResponse:
    """Return a cached/generated analytics CSV artifact or a retryable storage response."""
    if export_job.export_type != _ANALYTICS_SUMMARY_CSV_TYPE:
        raise AnalyticsSummaryCsvValidationError(
            "analytics summary CSV download only supports ANALYTICS_SUMMARY_CSV exports"
        )
    served = _serve_persisted_artifact_bytes(
        export_job=export_job,
        expected_export_type=_ANALYTICS_SUMMARY_CSV_TYPE,
        artifact_store=artifact_store,
    )
    if served is not None:
        csv_bytes, filename, content_type = served
        return _ExportDownloadArtifact(
            export_job=export_job,
            content=csv_bytes,
            filename=filename,
            content_type=content_type,
        )

    if export_job.status in _TERMINAL_EXPORT_JOB_STATUSES:
        # FIX: Terminal analytics CSV jobs must stop before source-row reads or
        # temporary artifact writes when no persisted artifact is available.
        return _terminal_export_conflict_response(export_job)

    csv_bytes = build_analytics_summary_csv(
        session=session,
        tenant_id=tenant_id,
        export_job=export_job,
        scope_channel_ids=scope_channel_ids,
    )
    filename = f"ums-analytics-summary-{export_job.month}-{export_job.scope_type}.csv"
    content_type = "text/csv"
    persisted_export_job, storage_failure_response = _persist_generated_export_artifact(
        repository=repository,
        artifact_store=artifact_store,
        export_job=export_job,
        content=csv_bytes,
        filename=filename,
        content_type=content_type,
    )
    if storage_failure_response is not None:
        return storage_failure_response
    persisted_export_job = _require_persisted_export_job(persisted_export_job)
    csv_bytes, filename, content_type = _require_persisted_artifact_bytes(
        export_job=persisted_export_job,
        expected_export_type=_ANALYTICS_SUMMARY_CSV_TYPE,
        artifact_store=artifact_store,
    )
    return _ExportDownloadArtifact(
        export_job=persisted_export_job,
        content=csv_bytes,
        filename=filename,
        content_type=content_type,
    )


def _persist_generated_export_artifact(
    *,
    repository: SqlAlchemyExportJobRepository,
    artifact_store: FileSystemExportArtifactStore,
    export_job: ExportJobEntry,
    content: bytes,
    filename: str,
    content_type: str,
) -> tuple[ExportJobEntry | None, JSONResponse | None]:
    """Save a generated artifact to the store and mark the export job as completed."""
    # ====================================================================
    # Purpose: First-time persistence of a generated export artifact. Jobs
    #   that are already in a terminal status keep their frozen metadata;
    #   the caller reloads persisted bytes for download.
    # Database/ORM: complete_artifact / fail_job on ExportJobORM.
    # Standards: Terminal jobs are append-only; concurrent re-downloads
    #   must not overwrite finalized metadata.
    # Blast Radius: Artifact filename, checksum, completed_at, audit.
    # ====================================================================
    if _has_completed_artifact(export_job):
        return export_job, None
    try:
        artifact = artifact_store.save(
            export_id=export_job.id,
            filename=filename,
            content_type=content_type,
            content=content,
        )
    except ExportArtifactStorageError:
        # Transient artifact-store failures (network blips, object-storage
        # outages, disk-full) should not move the export into a terminal
        # FAILED state — the next retry must be able to succeed once storage
        # recovers. Leave the persisted status alone and return 503 so the
        # caller can retry without first un-failing the job.
        logger.warning(
            "Export %s artifact storage unavailable; leaving job non-terminal for retry",
            export_job.id,
        )
        return None, JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Export artifact storage unavailable"},
        )
    try:
        completed_job = repository.complete_artifact(
            export_id=export_job.id,
            file_url=artifact.file_url,
            filename=artifact.filename,
            content_type=artifact.content_type,
            byte_size=artifact.byte_size,
            checksum_sha256=artifact.checksum_sha256,
        )
    except ExportJobTerminalStateError as exc:
        # ================================================================
        # Purpose: A concurrent writer finalized this export between our
        #   non-terminal read and the terminal-state guard inside
        #   complete_artifact. The artifact we just wrote occupies the
        #   same on-disk path, so keep it (do not _discard_saved_artifact)
        #   and return the now-terminal row instead of re-raising.
        # Database/ORM: Re-reads the export row to surface the
        #   first writer's persisted metadata.
        # Standards: Concurrent writers must not destroy each other's
        #   data on a race that the terminal-state guard already caught.
        # Blast Radius: Download response payload after a re-download race.
        # ================================================================
        logger.warning(
            "Export %s completed concurrently; preserving artifact: %s",
            export_job.id,
            exc,
        )
        latest_job = repository.get_job(export_job.id)
        if _has_completed_artifact(latest_job):
            return latest_job, None
        _discard_saved_artifact(artifact_store=artifact_store, file_url=artifact.file_url)
        return None, _terminal_export_conflict_response(latest_job)
    except Exception:
        logger.exception("Export artifact completion failed for export %s", export_job.id)
        _discard_saved_artifact(artifact_store=artifact_store, file_url=artifact.file_url)
        raise
    return completed_job, None


def _has_completed_artifact(export_job: ExportJobEntry) -> bool:
    """Return True if the export job has a persisted COMPLETED artifact."""
    return export_job.status == "COMPLETED" and export_job.file_url is not None


_DEFAULT_ARTIFACT_CONTENT_TYPES: dict[str, str] = {
    "FINANCE_EXCEL": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "EXECUTIVE_PDF": "application/pdf",
    "BRANDED_SLIDE_PACK": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    "ANALYTICS_SUMMARY_CSV": "text/csv",
}


def _serve_persisted_artifact_bytes(
    *,
    export_job: ExportJobEntry,
    expected_export_type: str,
    artifact_store: FileSystemExportArtifactStore,
) -> tuple[bytes, str, str] | None:
    """Return (bytes, filename, content_type) for a completed persisted artifact, or None."""
    # ====================================================================
    # Purpose: Return persisted bytes for a COMPLETED export so callers
    #   can short-circuit regenerating the workbook/PDF/PPTX on every
    #   download. The on-disk artifact is the source of truth once the
    #   job has been finalized; regenerating would drift from the
    #   persisted checksum recorded in audit metadata. If the row's
    #   stored artifact_filename / artifact_content_type are missing
    #   (legacy or manually-seeded rows), the helper derives both from
    #   the file_url and the expected export type so the caller can
    #   still serve the persisted bytes. If the on-disk file is missing
    #   for a COMPLETED row, the storage error propagates so the
    #   caller returns 503 instead of fresh bytes that no longer match
    #   the persisted checksum.
    # Database/ORM: None (FileSystemExportArtifactStore filesystem read).
    # Standards: Idempotent downloads, audit metadata integrity.
    # Blast Radius: Workbook/PDF/PPTX download response bytes.
    # ====================================================================
    if export_job.export_type != expected_export_type:
        return None
    if not _has_completed_artifact(export_job) or not export_job.file_url:
        return None
    artifact_bytes = artifact_store.read(file_url=export_job.file_url)
    filename = export_job.artifact_filename or export_job.file_url.rsplit("/", 1)[-1]
    content_type = export_job.artifact_content_type or _DEFAULT_ARTIFACT_CONTENT_TYPES.get(
        export_job.export_type, "application/octet-stream"
    )
    return artifact_bytes, filename, content_type


def _require_persisted_artifact_bytes(
    *,
    export_job: ExportJobEntry,
    expected_export_type: str,
    artifact_store: FileSystemExportArtifactStore,
) -> tuple[bytes, str, str]:
    """Return persisted artifact bytes or raise ExportArtifactStorageError if unavailable."""
    served = _serve_persisted_artifact_bytes(
        export_job=export_job,
        expected_export_type=expected_export_type,
        artifact_store=artifact_store,
    )
    if served is None:
        raise ExportArtifactStorageError("persisted artifact unavailable")
    return served


def _require_persisted_export_job(export_job: ExportJobEntry | None) -> ExportJobEntry:
    """Return the export job or raise RuntimeError if persistence returned None."""
    if export_job is None:
        raise RuntimeError("_persist_generated_export_artifact returned no export job")
    return export_job


def _discard_saved_artifact(
    *, artifact_store: FileSystemExportArtifactStore, file_url: str
) -> None:
    """Delete a saved artifact file, logging a warning on storage failure without re-raising."""
    try:
        artifact_store.delete(file_url=file_url)
    except ExportArtifactStorageError:
        logger.warning("Saved export artifact cleanup failed: %s", file_url, exc_info=True)


# ============================================================================
# Purpose: Build the finance workbook source summaries for preview/download
#   generation while keeping route handlers out of finance read orchestration.
# Database/ORM: Begins the platform session's REPEATABLE READ composed-read
#   snapshot (db/read_snapshot.py) and reads finance facts, payments, bank
#   entries, overrides, deductions, close state, account-allocation inputs,
#   and channel coverage through repositories on that ONE MVCC snapshot;
#   optional audit_logs connector edges stay on the tenant-lane request
#   session. No writes.
# Standards: Every route calling this builder gates before it runs, so denial
#   precedes the transaction begin (the platform ruling). The finance sources
#   feed PERSISTED, downloadable artifacts, so a writer committing mid-build
#   must not tear the exported totals — the same composed-read hazard class
#   proven dead on the dashboard reads, but landing in a file finance may
#   hand onward. The tenant-lane request session cannot host the snapshot
#   (the job lookup already opened its transaction), which is why the
#   finance reads ride the so-far-untouched platform session — the same
#   laning the revenue endpoints' finance repositories use. The snapshot
#   transaction is RELEASED (rollback — read-only, nothing to persist) as
#   soon as the last platform-lane read completes: artifact byte generation
#   and filesystem persistence run after this builder returns and must not
#   hold the snapshot open (idle-in-transaction timeouts, vacuum pressure);
#   on a mid-build source error the request teardown rolls the transaction
#   back instead, and no artifact work follows. Audit-derived
#   signal reads stay tenant-lane and OUTSIDE the snapshot by the recorded
#   residual ruling (their laning is an authorization boundary). Scoped
#   exports use the frozen channel set (deliberately gate-time: post-creation
#   org edits must not alter previously requested data); persisted artifacts
#   suppress caller-specific audit-derived alerts; preview-only
#   audit-derived alerts require VIEW_AUDIT_LOG and sensitive reason
#   payloads require VIEW_SENSITIVE_AUDIT_PAYLOADS.
# Blast Radius: Finance export numbers, smart-alert export disclosure, and
#   audit-derived alert visibility. No finance writes or auth weakening.
# Connections:
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the snapshot
#     begun before the first finance source read.
#   - File: backend/ums_smart_revenue/api/revenue.py -> dashboard smart-alert
#     data sources mirrored here for export parity.
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> failed
#     connector-run smart-alert read model.
#   - File: backend/ums_smart_revenue/reports/finance_workbook.py -> consumes
#     the returned summary bundle.
# ============================================================================
def _build_finance_source_summaries_for_export(
    *,
    context: _FinanceExportSourceContext,
) -> _FinanceExportSourceSummaries:
    """Resolve all finance source summaries (net revenue, payments, bank, alerts) for an export."""
    export_job = context.export_job
    user = context.user
    session = context.session
    platform_session = context.platform_session
    org_index = context.org_index
    group_registry = context.group_registry
    # FIX: One MVCC snapshot for every finance source read below — facts,
    # previous-month facts, overrides, payments, bank entries, close state,
    # deduction components, the allocation resolver's close probe, and the
    # missing-facts coverage pair — so a writer committing mid-build can no
    # longer tear a persisted export artifact's totals (REPEATABLE READ on
    # Postgres; db/read_snapshot.py holds the ruling). The audit-derived
    # signal reads below stay on the tenant-lane `session` by the recorded
    # residual ruling.
    begin_composed_read_snapshot(platform_session)
    # ====================================================================
    # Purpose: Resolve the YouTube channel set the export was issued for.
    #   Prefers the snapshot frozen on the export row so post-creation
    #   group/sector/company edits cannot alter previously requested data.
    #   Falls back to live resolution only for legacy rows that pre-date
    #   the snapshot column.
    # Database/ORM: Reads ExportJobORM.scope_channel_ids; org_index when
    #   no snapshot exists.
    # Standards: Finance number determinism.
    # Blast Radius: Finance export numbers and audit trail.
    # ====================================================================
    channel_ids = _resolved_export_channel_ids(
        export_job=export_job,
        org_index=org_index,
        group_registry=group_registry,
    )
    revenue_repository = SqlAlchemyRevenueFactRepository(platform_session)
    facts = revenue_repository.list_month_facts(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    previous_facts = revenue_repository.list_month_facts(
        month=previous_month(export_job.month),
        youtube_channel_ids=channel_ids,
    )
    manual_overrides = SqlAlchemyManualOverrideRepository(platform_session).list_month_overrides(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    payments = []
    bank_entries = []
    if channel_ids is None:
        payments = SqlAlchemyAdSensePaymentRepository(platform_session).list_month_payments(
            month=export_job.month
        )
        bank_entries = SqlAlchemyBankReconciliationRepository(platform_session).list_month_entries(
            month=export_job.month
        )
    close = SqlAlchemyFinanceMonthCloseRepository(platform_session).get(export_job.month)
    close_status = close.status if close is not None else export_job.month_lock_status

    # FIX: Exports must pass the same channel-direct deduction components and
    # account-allocation inputs as the net-revenue API; otherwise scoped export
    # net totals can drift from the API for missing-net channels.
    deduction_components = SqlAlchemyDeductionComponentRepository(
        platform_session
    ).list_month_components(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
        component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
    )
    # ====================================================================
    # Purpose: Resolve the month account allocation via the read-switch so a
    #   LOCKED month serves the committed snapshot (lossless), an OPEN month
    #   serves live compute, and a LOCKED month with no committed run falls
    #   back to live — capturing the source provenance for export disclosure.
    # Database/ORM: Reads the committed-allocation run + the finance month
    #   close status; live compute reads deduction/revenue/link repositories.
    # Standards: Finance number determinism; provenance is read-only and never
    #   mutates the source of truth or the allocation math.
    # Blast Radius: Finance export numbers + the account-allocation disclosure
    #   token; no auth/audit/write-path change.
    # ====================================================================
    account_result, account_allocation_provenance = resolve_month_account_allocation(
        month=export_job.month,
        session=platform_session,
        deduction_repository=SqlAlchemyDeductionComponentRepository(platform_session),
        revenue_repository=revenue_repository,
        link_repository=SqlAlchemyChannelAccountLinkRepository(platform_session),
        committed_repository=SqlAlchemyCommittedAllocationRepository(platform_session),
    )
    # FIX: the allocation orchestrator resolves month-wide, but a non-global
    # export must only contain its frozen channel set; filter allocation lines
    # to channel_ids so a company/sector/group export can never include
    # allocation rows or totals for channels outside the exported scope.
    # channel_ids is None only for global exports, which pass through unchanged.
    scoped_account_lines = filter_account_allocations_to_scope(account_result.lines, channel_ids)
    net_revenue = build_month_net_revenue_summary(
        month=export_job.month,
        facts=facts,
        manual_overrides=manual_overrides,
        deduction_components=deduction_components,
        account_allocations=scoped_account_lines,
        unallocated_account_issues=(
            account_result.unallocated if export_job.scope_type == "global" else None
        ),
    )
    payment_match = build_monthly_payment_match_summary(
        month=export_job.month,
        facts=facts,
        payments=payments,
        currency=export_job.currency,
    )
    bank_reconciliation = build_month_bank_reconciliation_summary(
        month=export_job.month,
        payments=payments,
        bank_entries=bank_entries,
    )
    # FIX: pass the same coverage data as the smart-alerts API so the
    # CHANNELS_MISSING_REVENUE_FACTS alert surfaces in the export summary
    # when the same month would surface it on the API. The export reads
    # the factless-channel set SCOPED to the export's frozen channel set
    # so a company/sector/group export never leaks channel ids outside
    # its scope (Kody #98 T13). Global exports pass channel_ids=None and
    # get the tenant-global view, matching the smart-alerts API endpoint.
    (
        missing_fact_channel_count,
        missing_fact_channel_sample,
    ) = missing_revenue_fact_channel_count_and_sample(
        platform_session,
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    # FIX (Qodo #202): the coverage pair above is the LAST platform-lane read,
    # so release the REPEATABLE READ transaction here instead of holding it
    # through artifact byte generation and filesystem persistence (the
    # platform session is request-scoped and would otherwise keep the
    # snapshot open until teardown - idle-in-transaction timeouts, vacuum
    # pressure). The transaction is read-only at this point (the routes write
    # their audit rows AFTER this builder returns), so rollback ends it with
    # provably nothing to persist - the fail-closed direction if a write ever
    # sneaks in above.
    platform_session.rollback()
    # FIX: preview may surface audit-derived connector smart-alert signals,
    # but persisted downloadable bytes must stay permission-invariant. Scoped
    # exports suppress these tenant-wide audit signals until source rows and
    # connector runs can be tied to the export's frozen channel set.
    audit_scope = AccessScope.global_scope()
    skipped_source_row_count: int
    skipped_source_rows_by_reason: dict[str, int]
    failed_connector_run_count: int
    failed_connector_runs_by_status: dict[str, int]
    if (
        context.include_audit_derived_alerts
        and channel_ids is None
        and has_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    ):
        include_sensitive_details = has_permission(
            user, Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS, audit_scope
        )
        skipped_source_row_count, skipped_source_rows_by_reason = (
            skipped_source_row_count_and_reasons(
                session,
                month=export_job.month,
                include_sensitive_details=include_sensitive_details,
            )
        )
        failed_connector_runs = SqlAlchemyAuditLogRepository(
            session,
            tenant_id=resolve_smart_alert_tenant_id(),
        ).connector_run_failure_summary(
            month=export_job.month,
        )
        failed_connector_run_count = failed_connector_runs.count
        failed_connector_runs_by_status = failed_connector_runs.by_status
    else:
        skipped_source_row_count = 0
        skipped_source_rows_by_reason = {}
        failed_connector_run_count = 0
        failed_connector_runs_by_status = {}
    smart_alerts = build_monthly_smart_alert_summary(
        month=export_job.month,
        finance=MonthlySmartAlertFinanceInputs(
            payment_match=payment_match,
            bank_reconciliation=bank_reconciliation,
            close_status=close_status,
            manual_overrides=manual_overrides,
        ),
        audit_signals=MonthlySmartAlertAuditSignals(
            missing_revenue_fact_channel_count=missing_fact_channel_count,
            missing_revenue_fact_channel_sample=missing_fact_channel_sample,
            skipped_source_row_count=skipped_source_row_count,
            skipped_source_rows_by_reason=skipped_source_rows_by_reason,
            failed_connector_run_count=failed_connector_run_count,
            failed_connector_runs_by_status=failed_connector_runs_by_status,
        ),
        trend_signals=MonthlySmartAlertTrendSignals(
            current_revenue_facts=facts,
            previous_revenue_facts=previous_facts,
        ),
    )
    return _FinanceExportSourceSummaries(
        net_revenue=net_revenue,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        smart_alerts=smart_alerts,
        account_allocation_provenance=account_allocation_provenance,
    )


# ============================================================================
# Purpose: Emit sensitive REVENUE_VIEWED and EXPORT_DOWNLOADED audit records
# for the exact persisted analytics CSV artifact returned to the caller, with
# revenue views scoped to the exported channel set.
# Database/ORM: audit_logs insert through AuditSink; ChannelGroupORM read only
# for legacy group exports without a frozen channel snapshot.
# Standards: Typed detail payload; artifact locator is redacted while checksum
# and size metadata remain audit-visible.
# Blast Radius: Audit trail for analytics CSV revenue reads and downloads.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py -> Audit persistence.
#   - File: backend/ums_smart_revenue/reports/exports.py -> Artifact metadata.
# ============================================================================
def _record_analytics_export_artifact_audit(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    export_job: ExportJobEntry,
    group_registry: ChannelGroupRegistryStore,
    artifact_type: str,
) -> tuple[AuditRecord, ...]:
    """Emit analytics export revenue-view and download audit events."""
    revenue_scopes = _audit_revenue_scopes_for_export(
        scope_type=export_job.scope_type,
        scope_id=export_job.scope_id,
        group_registry=group_registry,
        scope_channel_ids=export_job.scope_channel_ids,
        export_id=export_job.id,
    )
    base_details: dict[str, object] = _export_artifact_audit_details(
        export_job=export_job,
        artifact_type=artifact_type,
    )
    export_scope = AccessScope.export(export_job.id)
    revenue_records = tuple(
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="export_job",
            entity_id=export_job.id,
            scope=revenue_scope,
            details=dict(base_details),
        )
        for revenue_scope in revenue_scopes
    )
    download_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_DOWNLOADED,
        entity_type="export_job",
        entity_id=export_job.id,
        scope=export_scope,
        permission_override=Permission.EXPORT_ANALYTICS_REPORT,
        details=dict(base_details),
    )
    return (*revenue_records, download_record)


# ============================================================================
# Purpose: Emit sensitive revenue/payment/bank-reconciliation/download audit
#   records for a finance export artifact, plus a self-audit when export
#   generation reads audit_logs to derive connector-backed smart alerts.
# Database/ORM: audit_logs insert through AuditSink; ChannelGroupORM read only
#   for legacy group exports without a frozen channel snapshot.
# Standards: Typed detail payload; artifact locator is redacted while checksum
#   and size metadata remain audit-visible; audit-log self-audit marks
#   details_redacted only when SOURCE_ROWS_SKIPPED reasons were suppressed.
# Blast Radius: Finance export audit trail and connector smart-alert self-audit.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py -> Audit persistence.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> alert codes
#     summarized into the audit-log self-audit.
# ============================================================================
def _record_finance_export_artifact_audit(
    *,
    context: _FinanceExportAuditContext,
    artifact_type: str,
    include_download_event: bool,
    audit_summary: MonthlySmartAlertSummary | None = None,
) -> list[AuditRecord]:
    """Emit revenue, payment, bank-reconciliation, and optional download audit events."""
    audit_sink = context.audit_sink
    user = context.user
    export_job = context.export_job
    group_registry = context.group_registry
    revenue_scopes = _audit_revenue_scopes_for_export(
        scope_type=export_job.scope_type,
        scope_id=export_job.scope_id,
        group_registry=group_registry,
        scope_channel_ids=export_job.scope_channel_ids,
        export_id=export_job.id,
    )
    month_scope = AccessScope.finance_month(export_job.month)
    details = _export_artifact_audit_details(
        export_job=export_job,
        artifact_type=artifact_type,
    )
    audit_records = [
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="export_job",
            entity_id=export_job.id,
            scope=revenue_scope,
            details=details,
        )
        for revenue_scope in revenue_scopes
    ]
    audit_records.append(
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type="export_job",
            entity_id=export_job.id,
            scope=month_scope,
            details=details,
        )
    )
    if export_job.scope_type == "global":
        audit_records.append(
            record_audit_event(
                sink=audit_sink,
                actor=user,
                event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
                entity_type="export_job",
                entity_id=export_job.id,
                scope=month_scope,
                details=details,
            )
        )
    # FIX: Record an AUDIT_LOG_VIEWED self-audit when the export builder reads
    # audit_logs to derive connector-backed smart alerts. Mirrors the
    # /audit/events redaction-on-use pattern and the get_month_smart_alerts
    # self-audit so an export generated with VIEW_AUDIT_LOG leaves an audit
    # trail of its audit-derived read.
    audit_records.extend(
        _finance_export_connector_smart_alert_audit_records(
            audit_sink=audit_sink,
            user=user,
            export_job=export_job,
            artifact_type=artifact_type,
            audit_summary=audit_summary,
        )
    )
    if include_download_event:
        audit_records.append(
            record_audit_event(
                sink=audit_sink,
                actor=user,
                event_type=AuditEventType.EXPORT_DOWNLOADED,
                entity_type="export_job",
                entity_id=export_job.id,
                scope=AccessScope.export(export_job.id),
                details=details,
            )
        )
    return audit_records


# ============================================================================
# Purpose: Emit optional AUDIT_LOG_VIEWED self-audit records for finance export
#   connector smart-alert reads while keeping export audit orchestration narrow.
# Database/ORM: AuditSink append only; no direct SQLAlchemy reads.
# Standards: Preserves VIEW_AUDIT_LOG gating and sensitive skipped-row redaction.
# Blast Radius: Finance export audit trail only.
# Connections:
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> source summary
#     alert codes used to summarize connector-backed audit-derived reads.
# ============================================================================
def _finance_export_connector_smart_alert_audit_records(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    export_job: ExportJobEntry,
    artifact_type: str,
    audit_summary: MonthlySmartAlertSummary | None,
) -> tuple[AuditRecord, ...]:
    """Return the optional export self-audit record for connector smart-alert reads."""
    audit_scope = AccessScope.global_scope()
    if audit_summary is None or not has_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope):
        return ()
    connector_alert_codes = _connector_smart_alert_codes(audit_summary)
    return (
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.AUDIT_LOG_VIEWED,
            entity_type="audit_log_page",
            entity_id=f"{export_job.id}:connector_smart_alerts",
            scope=audit_scope,
            details=_finance_export_connector_smart_alert_audit_details(
                user=user,
                export_job=export_job,
                artifact_type=artifact_type,
                audit_scope=audit_scope,
                connector_alert_codes=connector_alert_codes,
            ),
        ),
    )


def _connector_smart_alert_codes(audit_summary: MonthlySmartAlertSummary) -> list[str]:
    """Return sorted connector-backed smart-alert codes from an export summary."""
    return sorted(
        alert.code for alert in audit_summary.alerts if alert.code in _CONNECTOR_SMART_ALERT_CODES
    )


def _finance_export_connector_smart_alert_audit_details(
    *,
    user: UserPrincipal,
    export_job: ExportJobEntry,
    artifact_type: str,
    audit_scope: AccessScope,
    connector_alert_codes: list[str],
) -> dict[str, object]:
    """Build stable audit details for finance export connector-alert reads."""
    source_rows_skipped_returned = "SOURCE_ROWS_SKIPPED" in connector_alert_codes
    connector_runs_failed_returned = "CONNECTOR_RUNS_FAILED" in connector_alert_codes
    details_redacted = source_rows_skipped_returned and not has_permission(
        user,
        Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS,
        audit_scope,
    )
    return {
        "event_type": AuditEventType.CONNECTOR_JOB_RUN.value,
        "entity_type": "finance_export",
        "entity_id": export_job.id,
        "month": export_job.month,
        "scope_type": export_job.scope_type,
        "scope_id": export_job.scope_id,
        "artifact_type": artifact_type,
        "connector_alert_codes": connector_alert_codes,
        "returned": int(bool(connector_alert_codes)),
        "source_rows_skipped_returned": int(source_rows_skipped_returned),
        "connector_runs_failed_returned": int(connector_runs_failed_returned),
        "details_redacted": details_redacted,
    }


def _artifact_metadata_audit_details(export_job: ExportJobEntry) -> dict[str, object]:
    """Build the artifact metadata dict for inclusion in audit event details."""
    details: dict[str, object] = {}
    if export_job.file_url is not None:
        details["artifact_locator_redacted"] = True
    artifact_metadata = {
        "artifact_filename": export_job.artifact_filename,
        "artifact_content_type": export_job.artifact_content_type,
        "artifact_byte_size": export_job.artifact_byte_size,
        "artifact_checksum_sha256": export_job.artifact_checksum_sha256,
    }
    missing_fields = [
        field_name for field_name, field_value in artifact_metadata.items() if field_value is None
    ]
    details["artifact_metadata_complete"] = not missing_fields
    if missing_fields:
        details["artifact_metadata_missing_fields"] = missing_fields
    else:
        details.update(artifact_metadata)
    return details


def _export_artifact_audit_details(
    *,
    export_job: ExportJobEntry,
    artifact_type: str,
) -> dict[str, object]:
    """Build the typed export artifact details payload used by audit events."""
    details: dict[str, object] = {
        "export_type": export_job.export_type,
        "artifact_type": artifact_type,
        "month": export_job.month,
        "scope_type": export_job.scope_type,
        "scope_id": export_job.scope_id,
    }
    if export_job.file_url:
        details.update(_artifact_metadata_audit_details(export_job))
    return details


def _audit_revenue_scopes_for_export(
    *,
    scope_type: str,
    scope_id: str | None,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
    export_id: str | None = None,
) -> tuple[AccessScope, ...]:
    """Derive the revenue access scopes used to record audit events for an export read."""
    # ====================================================================
    # Purpose: Derive audit scopes for an export read/download. For any
    #   non-global scoped export we prefer the channel snapshot frozen on
    #   the row so the audit trail mirrors the data actually returned.
    #   This protects against membership drift on company/sector/group
    #   between job creation and read. Falls back to live group membership
    #   only for legacy group rows; if the source group has been deleted
    #   and no snapshot exists, we record a single export-level audit
    #   instead of raising and blocking the read.
    # Database/ORM: ChannelGroupRegistryStore (read-only fallback).
    # Standards: Audit must succeed for any successfully-authorized read.
    # Blast Radius: REVENUE_VIEWED audit scope tracking.
    # ====================================================================
    if scope_type == "global":
        return (_access_scope_from_export_scope(scope_type, scope_id),)
    if scope_channel_ids is not None:
        return tuple(AccessScope.channel(channel_id) for channel_id in scope_channel_ids)
    if scope_type != "group":
        return (_access_scope_from_export_scope(scope_type, scope_id),)
    if not scope_id:
        raise ExportJobValidationError("scope_id is required for export scope_type: group")
    # The earlier `scope_channel_ids is not None` branch above already returns
    # for any non-global scope when a snapshot is present, so by the time we
    # reach the group path scope_channel_ids is guaranteed to be None and we
    # fall back to live group membership.
    group = group_registry.get_group(scope_id)
    if group is None:
        if export_id is None:
            raise KeyError(f"Group not found: {scope_id}")
        return (AccessScope.export(export_id),)
    return tuple(AccessScope.channel(channel_id) for channel_id in group.channel_ids)


def _require_export_permissions(
    *,
    user: UserPrincipal,
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Assert the caller holds the appropriate export and view permissions for the given scope."""
    finance_export = is_finance_export_type(export_type)
    export_permission = _audit_permission_for_export_type(export_type)
    view_permission = Permission.VIEW_REVENUE if finance_export else Permission.VIEW_ANALYTICS
    _require_export_scope_permissions(
        user=user,
        export_permission=export_permission,
        view_permission=view_permission,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
        scope_channel_ids=scope_channel_ids,
    )


def _audit_permission_for_export_type(export_type: str) -> Permission:
    """Return the export permission required for the given export type."""
    if is_finance_export_type(export_type):
        return Permission.EXPORT_REVENUE_REPORT
    return Permission.EXPORT_ANALYTICS_REPORT


def _require_export_access_permissions(
    *,
    user: UserPrincipal,
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    month: str,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Dispatch to finance or analytics permission checks based on the export type."""
    if is_finance_export_type(export_type):
        _require_finance_export_artifact_permissions(
            user=user,
            scope_type=scope_type,
            scope_id=scope_id,
            month=month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=scope_channel_ids,
        )
        return
    _require_export_permissions(
        user=user,
        export_type=export_type,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
        scope_channel_ids=scope_channel_ids,
    )


def _require_export_scope_permissions(
    *,
    user: UserPrincipal,
    export_permission: Permission,
    view_permission: Permission,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Enforce export and view permissions across all channels in the export scope."""
    # ====================================================================
    # Purpose: Authorize an export read or download against the frozen
    #   channel snapshot when one exists for any non-global scope. This
    #   keeps permission decisions in lockstep with the data the export
    #   actually returns: post-creation membership edits to the source
    #   group, sector, or company cannot widen or narrow access on
    #   previously persisted exports.
    # Database/ORM: ExportJobORM.scope_channel_ids, ChannelGroupORM.
    # Standards: Authorization must mirror the resolved data set.
    # Blast Radius: Export read/download access control.
    # ====================================================================
    if scope_channel_ids is not None and scope_type != "global":
        if not scope_channel_ids:
            raise ExportJobValidationError("scoped exports require at least one channel")
        for channel_id in scope_channel_ids:
            channel_scope = AccessScope.channel(channel_id)
            require_permission(user, export_permission, channel_scope, org_index)
            require_permission(user, view_permission, channel_scope, org_index)
        return

    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError("scope_id is required for export scope_type: group")
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        if not group.channel_ids:
            raise ExportJobValidationError("group exports require at least one channel")
        for channel_id in group.channel_ids:
            channel_scope = AccessScope.channel(channel_id)
            require_permission(user, export_permission, channel_scope, org_index)
            require_permission(user, view_permission, channel_scope, org_index)
        return

    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    require_permission(user, export_permission, target_scope, org_index)
    require_permission(user, view_permission, target_scope, org_index)


# ============================================================================
# Purpose: Authorize analytics CSV artifact reads across the frozen export
# channel set, including finance visibility because the CSV carries revenue
# amounts from google_revenue_source_rows.
# Database/ORM: ChannelGroupORM may be read for legacy group exports without
# a frozen channel snapshot.
# Standards: Fail-closed boundary check before source-row reads or artifact
# writes; no audit side effects on denial.
# Blast Radius: Authorization for analytics CSV downloads and finance amounts.
# Connections:
#   - File: backend/ums_smart_revenue/auth/permissions.py -> Permission catalog.
#   - File: backend/ums_smart_revenue/reports/analytics_summary_csv.py -> Revenue rows.
# ============================================================================
def _require_analytics_export_artifact_permissions(
    *,
    user: UserPrincipal,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Assert analytics export, analytics view, and revenue view permissions."""
    # FIX: A direct grant on AccessScope.group(id) does not prove access to each
    # frozen member channel. Group CSV downloads must fall through to the
    # snapshot/channel checks below so mixed-company revenue rows stay protected.
    if scope_type != "group" and _has_export_scope_permissions(
        user=user,
        permissions=_ANALYTICS_SUMMARY_CSV_REQUIRED_PERMISSIONS,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
    ):
        return

    _require_export_scope_permissions(
        user=user,
        export_permission=Permission.EXPORT_ANALYTICS_REPORT,
        view_permission=Permission.VIEW_ANALYTICS,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
        scope_channel_ids=scope_channel_ids,
    )
    _require_export_scope_view_permission(
        user=user,
        view_permission=Permission.VIEW_REVENUE,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
        scope_channel_ids=scope_channel_ids,
    )


# ============================================================================
# Purpose: Check whether the caller still holds the declared export scope grant
# for a queued analytics CSV artifact before falling back to channel snapshots.
# Database/ORM: None.
# Standards: Fail-closed authorization helper; route remains the only HTTP boundary.
# Blast Radius: Authorization for delayed analytics CSV downloads after org drift.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> Scope permission engine.
#   - File: tests/api/test_exports_api.py -> Org-drift download regression.
# ============================================================================
def _has_export_scope_permissions(
    *,
    user: UserPrincipal,
    permissions: tuple[Permission, ...],
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
) -> bool:
    """Return whether user holds every permission in `permissions` for the export scope."""
    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    return all(
        has_permission(user, permission, target_scope, org_index) for permission in permissions
    )


def _require_export_scope_view_permission(
    *,
    user: UserPrincipal,
    view_permission: Permission,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Enforce one view permission across all channels in the export scope."""
    # ============================================================================
    # Purpose: Apply a single scoped read permission to the same frozen channel
    # set used by export generation without requiring an additional export action.
    # Database/ORM: ChannelGroupORM may be read for legacy group exports without
    # a frozen channel snapshot.
    # Standards: Shared fail-closed scope semantics with _require_export_scope_permissions.
    # Blast Radius: Authorization only; no finance math, artifact, or audit writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/policy.py -> Scope inheritance.
    #   - File: backend/ums_smart_revenue/org/channel_groups.py -> Group membership.
    # ============================================================================
    if scope_channel_ids is not None and scope_type != "global":
        if not scope_channel_ids:
            raise ExportJobValidationError("scoped exports require at least one channel")
        for channel_id in scope_channel_ids:
            require_permission(user, view_permission, AccessScope.channel(channel_id), org_index)
        return

    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError("scope_id is required for export scope_type: group")
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        if not group.channel_ids:
            raise ExportJobValidationError("group exports require at least one channel")
        for channel_id in group.channel_ids:
            require_permission(user, view_permission, AccessScope.channel(channel_id), org_index)
        return

    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    require_permission(user, view_permission, target_scope, org_index)


def _require_finance_export_artifact_permissions(
    *,
    user: UserPrincipal,
    scope_type: str,
    scope_id: str | None,
    month: str,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
) -> None:
    """Assert full finance artifact permissions for payments and bank-reconciliation."""
    _require_export_scope_permissions(
        user=user,
        export_permission=Permission.EXPORT_REVENUE_REPORT,
        view_permission=Permission.VIEW_REVENUE,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
        scope_channel_ids=scope_channel_ids,
    )
    month_scope = AccessScope.finance_month(month)
    require_permission(
        user,
        Permission.VIEW_FINALIZED_PAYMENTS,
        month_scope,
        org_index,
    )
    require_permission(
        user,
        Permission.VIEW_BANK_RECONCILIATION,
        month_scope,
        org_index,
    )


def _resolved_export_channel_ids(
    *,
    export_job,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> set[str] | None:
    """Return the channel ID set for the export, preferring the frozen snapshot over live lookup."""
    if export_job.scope_type == "global":
        return None
    snapshot = getattr(export_job, "scope_channel_ids", None)
    if snapshot is not None:
        return set(snapshot)
    return _channel_ids_for_export_scope(
        scope_type=export_job.scope_type,
        scope_id=export_job.scope_id,
        org_index=org_index,
        group_registry=group_registry,
    )


def _channel_snapshot_tuple(channel_ids: set[str] | None) -> tuple[str, ...] | None:
    """Convert a resolved channel set to the tuple shape used by authorization helpers."""
    return None if channel_ids is None else tuple(sorted(channel_ids))


# ============================================================================
# Purpose: Resolve a request-time channel snapshot and mask analytics CSV scope
#   lookup details for callers missing the requested revenue/analytics scope.
# Database/ORM: OrgAccessIndex and ChannelGroupRegistryStore read models.
# Standards: Fail-closed authorization before exposing org-scope existence.
# Blast Radius: Analytics CSV authorization, export audit scope, persisted jobs.
# Connections:
#   - File: backend/ums_smart_revenue/api/exports.py -> request_export caller.
#   - File: tests/api/test_exports_api.py -> CSV permission masking regressions.
# ============================================================================
def _resolve_export_scope_snapshot(
    *,
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    analytics_csv_lookup_denial_permission: Permission | None,
) -> tuple[str, ...] | None:
    """Resolve the one channel snapshot used for auth, audit, and persistence."""
    denial_permission = _analytics_csv_scope_denial_permission(
        export_type=export_type,
        analytics_csv_lookup_denial_permission=analytics_csv_lookup_denial_permission,
    )
    try:
        snapshot = _channel_ids_for_export_scope(
            scope_type=scope_type,
            scope_id=scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
    except KeyError:
        if denial_permission is not None:
            raise_missing_permission(denial_permission)
        raise
    snapshot_tuple = _channel_snapshot_tuple(snapshot)
    if denial_permission is not None and snapshot_tuple is not None and not snapshot_tuple:
        # FIX: Known-but-empty org scopes should not reveal existence to a
        # caller missing the requested analytics CSV permission scope.
        raise_missing_permission(denial_permission)
    return snapshot_tuple


def _analytics_csv_scope_denial_permission(
    *,
    export_type: str,
    analytics_csv_lookup_denial_permission: Permission | None,
) -> Permission | None:
    """Return the permission used to mask analytics CSV scope lookup details."""
    if export_type != _ANALYTICS_SUMMARY_CSV_TYPE:
        return None
    return analytics_csv_lookup_denial_permission


def _channel_ids_for_export_scope(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> set[str] | None:
    """Resolve the live channel ID set for the given scope type and ID."""
    if scope_type == "global":
        return None
    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError("scope_id is required for export scope_type: group")
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        return set(group.channel_ids)
    if not scope_id:
        raise ExportJobValidationError(f"scope_id is required for export scope_type: {scope_type}")
    if scope_type == "sector":
        # Raise KeyError only when the sector itself is unknown (404). When the
        # sector exists but currently has no channels, return an empty set so
        # the downstream "scoped exports require at least one channel" path
        # produces a 422 with a clearer message than a generic 404.
        known_sector_ids = set(org_index.channel_sector.values()) | set(
            org_index.company_sector.values()
        )
        if scope_id not in known_sector_ids:
            raise KeyError(f"Sector not found: {scope_id}")
        return {
            channel_id
            for channel_id, sector_id in org_index.channel_sector.items()
            if sector_id == scope_id
        }
    if scope_type == "company":
        known_company_ids = set(org_index.channel_company.values()) | set(org_index.company_sector)
        if scope_id not in known_company_ids:
            raise KeyError(f"Company not found: {scope_id}")
        return {
            channel_id
            for channel_id, company_id in org_index.channel_company.items()
            if company_id == scope_id
        }
    if scope_type == "channel":
        _require_known_channel_scope(scope_id, org_index)
        return {scope_id}
    raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")


def _require_known_channel_scope(scope_id: str, org_index: OrgAccessIndex) -> None:
    """Raise KeyError if the channel ID is not present in the org index."""
    if scope_id not in org_index.channel_company and scope_id not in org_index.channel_sector:
        raise KeyError(f"Channel not found: {scope_id}")


# ============================================================================
# Purpose: Fail analytics summary CSV creation when required grants are absent,
# and defer same-tenant child-coverage decisions until the frozen channel
# snapshot is available.
# Database/ORM: None; relies on OrgAccessIndex authorization mappings only.
# Standards: Fail-closed authorization, safe HTTP errors, route boundary helper.
# Blast Radius: Authorization for revenue-bearing analytics CSV export creation.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> Scope containment.
#   - File: tests/api/test_exports_api.py -> Scope-probe regression coverage.
# ============================================================================
def _require_analytics_csv_permissions_before_scope_lookup(
    *,
    user: UserPrincipal,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
) -> Permission | None:
    """Return a scoped-lookup denial permission to normalize 404s to 403s."""
    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    lookup_denial_permission: Permission | None = None
    for permission in _ANALYTICS_SUMMARY_CSV_REQUIRED_PERMISSIONS:
        if not _has_permission_assignment(user, permission):
            # FIX: Deny missing export/analytics/revenue grants before resolving
            # scoped membership, so scope existence cannot be probed through 404s.
            raise_missing_permission(permission)
        if lookup_denial_permission is None and not _has_csv_lookup_permission(
            user=user,
            permission=permission,
            target_scope=target_scope,
            org_index=org_index,
        ):
            lookup_denial_permission = permission
    return lookup_denial_permission


# ============================================================================
# Purpose: Decide whether analytics CSV creation may expose scope lookup results
# before a group snapshot is available.
# Database/ORM: None; evaluates in-memory permission grants and OrgAccessIndex.
# Standards: Fail-closed authorization helper; route remains the HTTP boundary.
# Blast Radius: Analytics CSV export creation and group existence masking.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> Scope containment.
#   - File: tests/api/test_exports_api.py -> Group lookup probe regression.
# ============================================================================
def _has_csv_lookup_permission(
    *,
    user: UserPrincipal,
    permission: Permission,
    target_scope: AccessScope,
    org_index: OrgAccessIndex,
) -> bool:
    """Return whether a grant is enough to expose pre-snapshot scope lookup details."""
    if target_scope.type != ScopeType.GROUP:
        return has_permission(user, permission, target_scope, org_index)
    # FIX: Direct group grants do not prove access to every member channel until
    # the group snapshot has been loaded. Ignore that exact group grant for the
    # lookup mask so unknown group IDs cannot be distinguished from unauthorized
    # groups by users who only hold group-scoped CSV permissions.
    return _has_permission_without_exact_scope(
        user=user,
        permission=permission,
        excluded_scope=target_scope,
        org_index=org_index,
    )


def _has_permission_without_exact_scope(
    *,
    user: UserPrincipal,
    permission: Permission,
    excluded_scope: AccessScope,
    org_index: OrgAccessIndex,
) -> bool:
    """Return whether a permission is held through any scope except one exact scope."""
    if user.disabled:
        return False
    for grant in user.direct_permissions:
        if grant.scope == excluded_scope:
            continue
        if (
            grant.active
            and grant.permission == permission
            and org_index.contains(
                grant.scope,
                excluded_scope,
            )
        ):
            return True
    for assignment in user.role_assignments:
        if assignment.scope == excluded_scope:
            continue
        role_permissions = ROLE_PERMISSIONS.get(assignment.role, frozenset())
        if (
            assignment.active
            and permission in role_permissions
            and org_index.contains(assignment.scope, excluded_scope)
        ):
            return True
    return False


def _access_scope_from_export_scope(scope_type: str, scope_id: str | None) -> AccessScope:
    """Convert export scope_type and scope_id to an AccessScope object."""
    if scope_type == "global":
        if scope_id is not None:
            raise ExportJobValidationError("scope_id must be omitted for global exports")
        return AccessScope.global_scope()
    if not scope_id:
        raise ExportJobValidationError(f"scope_id is required for export scope_type: {scope_type}")
    if scope_type == "sector":
        return AccessScope.sector(scope_id)
    if scope_type == "company":
        return AccessScope.company(scope_id)
    if scope_type == "channel":
        return AccessScope.channel(scope_id)
    if scope_type == "group":
        return AccessScope.group(scope_id)
    raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")


def _has_any_export_permission(user: UserPrincipal) -> bool:
    """Return True if the user holds at least one export permission (analytics or revenue)."""
    export_permissions = {
        Permission.EXPORT_ANALYTICS_REPORT,
        Permission.EXPORT_REVENUE_REPORT,
    }
    return any(_has_permission_assignment(user, permission) for permission in export_permissions)


def _has_permission_assignment(
    user: UserPrincipal,
    permission: Permission,
) -> bool:
    """Return True if the user has an active direct grant or role assignment for the permission."""
    if user.disabled:
        return False
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            return True
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset()):
            return True
    return False


def _list_authorized_export_jobs(
    *,
    repository: SqlAlchemyExportJobRepository,
    user: UserPrincipal,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
    limit: int,
    offset: int,
    max_scan_pages: int = MAX_AUTHORIZED_EXPORT_JOB_SCAN_PAGES,
) -> tuple[list[ExportJobEntry], bool]:
    """Page through export jobs and return only those the caller is authorized to access."""
    if max_scan_pages < 1:
        raise ExportJobValidationError("max_scan_pages must be positive")
    items: list[ExportJobEntry] = []
    skipped = 0
    scan_offset = 0
    scan_pages = 0
    scanned_items = 0
    last_page_has_more = False
    while len(items) <= limit and scan_pages < max_scan_pages:
        page = repository.list_jobs(
            requested_by=user.user_id,
            limit=MAX_EXPORT_JOB_PAGE_SIZE,
            offset=scan_offset,
        )
        scan_pages += 1
        scanned_items += len(page.items)
        last_page_has_more = page.has_more
        if not page.items:
            last_page_has_more = False
            break
        for export_job in page.items:
            if not _can_access_export_job(
                user=user,
                export_job=export_job,
                org_index=org_index,
                group_registry=group_registry,
            ):
                continue
            if skipped < offset:
                skipped += 1
                continue
            items.append(export_job)
            if len(items) > limit:
                break
        if not page.has_more or len(items) > limit:
            break
        scan_offset += len(page.items)
    scan_truncated = last_page_has_more and len(items) <= limit and scan_pages >= max_scan_pages
    if scan_truncated:
        logger.warning(
            "metric=export_job_scan_truncated scanned_pages=%s "
            "scanned_items=%s limit=%s offset=%s max_scan_pages=%s",
            scan_pages,
            scanned_items,
            limit,
            offset,
            max_scan_pages,
        )
    return items[:limit], len(items) > limit or scan_truncated


def _can_access_export_job(
    *,
    user: UserPrincipal,
    export_job: ExportJobEntry,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> bool:
    """Return True if the user passes access-permission checks for the export job."""
    try:
        _require_export_access_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
            scope_channel_ids=export_job.scope_channel_ids,
        )
    except (ExportJobValidationError, HTTPException, KeyError):
        return False
    return True
