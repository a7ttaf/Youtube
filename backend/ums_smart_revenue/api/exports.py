import logging
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationValidationError,
    MonthBankReconciliationSummary,
    SqlAlchemyBankReconciliationRepository,
    build_month_bank_reconciliation_summary,
)
from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideValidationError,
    SqlAlchemyManualOverrideRepository,
)
from ums_smart_revenue.finance.month_close import SqlAlchemyFinanceMonthCloseRepository
from ums_smart_revenue.finance.net_revenue import (
    MonthNetRevenueSummary,
    NetRevenueValidationError,
    build_month_net_revenue_summary,
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
from ums_smart_revenue.finance.smart_alerts import (
    MonthlySmartAlertSummary,
    build_monthly_smart_alert_summary,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
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

router = APIRouter(prefix="/exports", tags=["exports"])
EXPORT_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
logger = logging.getLogger(__name__)
MAX_AUTHORIZED_EXPORT_JOB_SCAN_PAGES = 10


@dataclass(frozen=True)
class _FinanceExportSourceSummaries:
    net_revenue: MonthNetRevenueSummary
    payment_match: MonthlyPaymentMatchSummary
    bank_reconciliation: MonthBankReconciliationSummary
    smart_alerts: MonthlySmartAlertSummary


class ExportRequest(BaseModel):
    export_type: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    month: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=1)
    include_confidence_notes: bool = True
    include_manual_override_notes: bool = True
    reason: str = Field(min_length=1)

    @field_validator(
        "export_type", "scope_type", "month", "currency", "reason", mode="before"
    )
    @classmethod
    def strip_required_strings(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("scope_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_export_job_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyExportJobRepository:
    return SqlAlchemyExportJobRepository(session)


def current_export_artifact_store() -> FileSystemExportArtifactStore:
    return FileSystemExportArtifactStore.from_environment()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def request_export(
    payload: ExportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    try:
        # ================================================================
        # Purpose: Resolve the channel set ONCE so the authorization
        #   check, the audit trail, and the persisted snapshot all see
        #   the same membership. Re-resolving live group/sector/company
        #   data twice creates a window where a concurrent edit could
        #   authorize the request against one set and persist another.
        # Database/ORM: org_index, ChannelGroupRegistryStore.
        # Standards: Authorization must mirror persisted data.
        # Blast Radius: Group export authorization and snapshot drift.
        # ================================================================
        snapshot = _channel_ids_for_export_scope(
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
        snapshot_tuple = (
            tuple(sorted(snapshot)) if snapshot is not None else None
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
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_JOB_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
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
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id, requested_by=user.user_id)
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
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
    details = {
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
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
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
            export_job=export_job,
            session=session,
            org_index=org_index,
            group_registry=group_registry,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
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
        audit_sink=audit_sink,
        user=user,
        export_job=export_job,
        group_registry=group_registry,
        artifact_type="finance_workbook_preview",
        include_download_event=False,
    )
    response = preview.to_api()
    response["audit_events"] = [audit_record_to_api(record) for record in audit_records]
    return response


@router.get("/{export_id}/finance-workbook.xlsx")
def download_finance_workbook(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
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
                export_job=export_job,
                session=session,
                org_index=org_index,
                group_registry=group_registry,
            )
            workbook_bytes = build_finance_workbook_xlsx(preview)
            filename = f"ums-finance-{export_job.month}-{export_job.scope_type}.xlsx"
            content_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=workbook_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(export_job)
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
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
        audit_sink=audit_sink,
        user=user,
        export_job=export_job,
        group_registry=group_registry,
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
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
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
                export_job=export_job,
                session=session,
                org_index=org_index,
                group_registry=group_registry,
            )
            report = build_executive_pdf_report(
                export_job=export_job,
                net_revenue=source_summaries.net_revenue,
                payment_match=source_summaries.payment_match,
                bank_reconciliation=source_summaries.bank_reconciliation,
                smart_alerts=source_summaries.smart_alerts,
            )
            pdf_bytes = build_executive_pdf_bytes(report)
            filename = f"ums-executive-{export_job.month}-{export_job.scope_type}.pdf"
            content_type = "application/pdf"
            export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=pdf_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(export_job)
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
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
        audit_sink=audit_sink,
        user=user,
        export_job=export_job,
        group_registry=group_registry,
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
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    artifact_store: Annotated[
        FileSystemExportArtifactStore, Depends(current_export_artifact_store)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
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
                export_job=export_job,
                session=session,
                org_index=org_index,
                group_registry=group_registry,
            )
            report = build_branded_slide_pack_report(
                export_job=export_job,
                net_revenue=source_summaries.net_revenue,
                payment_match=source_summaries.payment_match,
                bank_reconciliation=source_summaries.bank_reconciliation,
                smart_alerts=source_summaries.smart_alerts,
            )
            pptx_bytes = build_branded_slide_pack_pptx(report)
            filename = f"ums-branded-{export_job.month}-{export_job.scope_type}.pptx"
            content_type = (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
            export_job, storage_failure_response = _persist_generated_export_artifact(
                repository=repository,
                artifact_store=artifact_store,
                export_job=export_job,
                content=pptx_bytes,
                filename=filename,
                content_type=content_type,
            )
            if storage_failure_response is not None:
                return storage_failure_response
            export_job = _require_persisted_export_job(export_job)
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExportArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export artifact storage unavailable",
        ) from exc
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
        BrandedSlidePackValidationError,
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
        audit_sink=audit_sink,
        user=user,
        export_job=export_job,
        group_registry=group_registry,
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
    export_job,
    session: Session,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> FinanceWorkbookPreview:
    source_summaries = _build_finance_source_summaries_for_export(
        export_job=export_job,
        session=session,
        org_index=org_index,
        group_registry=group_registry,
    )
    return build_finance_workbook_preview(
        export_job=export_job,
        net_revenue=source_summaries.net_revenue,
        payment_match=source_summaries.payment_match,
        bank_reconciliation=source_summaries.bank_reconciliation,
        smart_alerts=source_summaries.smart_alerts,
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
        try:
            repository.fail_job(
                export_id=export_job.id,
                failure_reason="artifact storage unavailable",
            )
        except ExportJobTerminalStateError as terminal_exc:
            # Another writer already finalized this export. Storage is still
            # unavailable for this caller, so we surface a 503 without
            # overriding the persisted terminal status.
            logger.warning(
                "Export %s reached terminal status during storage failure: %s",
                export_job.id,
                terminal_exc,
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
        return None, JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    "Export job is already in terminal status "
                    f"{latest_job.status}"
                )
            },
        )
    except Exception:
        logger.exception("Export artifact completion failed for export %s", export_job.id)
        _discard_saved_artifact(artifact_store=artifact_store, file_url=artifact.file_url)
        raise
    return completed_job, None


def _has_completed_artifact(export_job: ExportJobEntry) -> bool:
    return export_job.status == "COMPLETED" and export_job.file_url is not None


_DEFAULT_ARTIFACT_CONTENT_TYPES: dict[str, str] = {
    "FINANCE_EXCEL": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
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
    content_type = (
        export_job.artifact_content_type
        or _DEFAULT_ARTIFACT_CONTENT_TYPES.get(
            export_job.export_type, "application/octet-stream"
        )
    )
    return artifact_bytes, filename, content_type


def _require_persisted_artifact_bytes(
    *,
    export_job: ExportJobEntry,
    expected_export_type: str,
    artifact_store: FileSystemExportArtifactStore,
) -> tuple[bytes, str, str]:
    served = _serve_persisted_artifact_bytes(
        export_job=export_job,
        expected_export_type=expected_export_type,
        artifact_store=artifact_store,
    )
    if served is None:
        raise ExportArtifactStorageError("persisted artifact unavailable")
    return served


def _require_persisted_export_job(export_job: ExportJobEntry | None) -> ExportJobEntry:
    if export_job is None:
        raise RuntimeError("_persist_generated_export_artifact returned no export job")
    return export_job


def _discard_saved_artifact(
    *, artifact_store: FileSystemExportArtifactStore, file_url: str
) -> None:
    try:
        artifact_store.delete(file_url=file_url)
    except ExportArtifactStorageError:
        logger.warning("Saved export artifact cleanup failed: %s", file_url, exc_info=True)


def _build_finance_source_summaries_for_export(
    *,
    export_job,
    session: Session,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> _FinanceExportSourceSummaries:
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
    revenue_repository = SqlAlchemyRevenueFactRepository(session)
    facts = revenue_repository.list_month_facts(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    previous_facts = revenue_repository.list_month_facts(
        month=_previous_month(export_job.month),
        youtube_channel_ids=channel_ids,
    )
    manual_overrides = SqlAlchemyManualOverrideRepository(session).list_month_overrides(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    payments = []
    bank_entries = []
    if channel_ids is None:
        payments = SqlAlchemyAdSensePaymentRepository(session).list_month_payments(
            month=export_job.month
        )
        bank_entries = SqlAlchemyBankReconciliationRepository(
            session
        ).list_month_entries(month=export_job.month)
    close = SqlAlchemyFinanceMonthCloseRepository(session).get(export_job.month)
    close_status = close.status if close is not None else export_job.month_lock_status

    net_revenue = build_month_net_revenue_summary(
        month=export_job.month,
        facts=facts,
        manual_overrides=manual_overrides,
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
    smart_alerts = build_monthly_smart_alert_summary(
        month=export_job.month,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        close_status=close_status,
        manual_overrides=manual_overrides,
        current_revenue_facts=facts,
        previous_revenue_facts=previous_facts,
    )
    return _FinanceExportSourceSummaries(
        net_revenue=net_revenue,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        smart_alerts=smart_alerts,
    )


def _record_finance_export_artifact_audit(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    export_job,
    group_registry: ChannelGroupRegistryStore,
    artifact_type: str,
    include_download_event: bool,
):
    revenue_scopes = _audit_revenue_scopes_for_export(
        scope_type=export_job.scope_type,
        scope_id=export_job.scope_id,
        group_registry=group_registry,
        scope_channel_ids=export_job.scope_channel_ids,
        export_id=export_job.id,
    )
    month_scope = AccessScope.finance_month(export_job.month)
    details = {
        "export_type": export_job.export_type,
        "artifact_type": artifact_type,
        "month": export_job.month,
        "scope_type": export_job.scope_type,
        "scope_id": export_job.scope_id,
    }
    if export_job.file_url:
        details.update(_artifact_metadata_audit_details(export_job))
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
    if export_job.scope_type == "global":
        audit_records.extend(
            [
                record_audit_event(
                    sink=audit_sink,
                    actor=user,
                    event_type=AuditEventType.PAYMENT_VIEWED,
                    entity_type="export_job",
                    entity_id=export_job.id,
                    scope=month_scope,
                    details=details,
                ),
                record_audit_event(
                    sink=audit_sink,
                    actor=user,
                    event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
                    entity_type="export_job",
                    entity_id=export_job.id,
                    scope=month_scope,
                    details=details,
                ),
            ]
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


def _artifact_metadata_audit_details(export_job: ExportJobEntry) -> dict[str, object]:
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


def _audit_revenue_scopes_for_export(
    *,
    scope_type: str,
    scope_id: str | None,
    group_registry: ChannelGroupRegistryStore,
    scope_channel_ids: tuple[str, ...] | None = None,
    export_id: str | None = None,
) -> tuple[AccessScope, ...]:
    # ====================================================================
    # Purpose: Derive audit scopes for an export read/download. For group
    #   exports we prefer the channel snapshot frozen on the row so the
    #   audit trail mirrors the data actually returned. Falls back to
    #   live group membership only for legacy rows; if the source group
    #   has been deleted and no snapshot exists, we record a single
    #   export-level audit instead of raising and blocking the read.
    # Database/ORM: ChannelGroupRegistryStore (read-only fallback).
    # Standards: Audit must succeed for any successfully-authorized read.
    # Blast Radius: REVENUE_VIEWED audit scope tracking.
    # ====================================================================
    if scope_type != "group":
        return (_access_scope_from_export_scope(scope_type, scope_id),)
    if not scope_id:
        raise ExportJobValidationError(
            "scope_id is required for export scope_type: group"
        )
    if scope_channel_ids is not None:
        return tuple(
            AccessScope.channel(channel_id) for channel_id in scope_channel_ids
        )
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
    finance_export = is_finance_export_type(export_type)
    export_permission = _audit_permission_for_export_type(export_type)
    view_permission = (
        Permission.VIEW_REVENUE if finance_export else Permission.VIEW_ANALYTICS
    )
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
            raise ExportJobValidationError(
                "scoped exports require at least one channel"
            )
        for channel_id in scope_channel_ids:
            channel_scope = AccessScope.channel(channel_id)
            _require_permission(user, export_permission, channel_scope, org_index)
            _require_permission(user, view_permission, channel_scope, org_index)
        return

    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError(
                "scope_id is required for export scope_type: group"
            )
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        if not group.channel_ids:
            raise ExportJobValidationError(
                "group exports require at least one channel"
            )
        for channel_id in group.channel_ids:
            channel_scope = AccessScope.channel(channel_id)
            _require_permission(user, export_permission, channel_scope, org_index)
            _require_permission(user, view_permission, channel_scope, org_index)
        return

    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    _require_permission(user, export_permission, target_scope, org_index)
    _require_permission(user, view_permission, target_scope, org_index)


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
    _require_permission(
        user,
        Permission.VIEW_FINALIZED_PAYMENTS,
        month_scope,
        org_index,
    )
    _require_permission(
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


def _channel_ids_for_export_scope(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> set[str] | None:
    if scope_type == "global":
        return None
    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError(
                "scope_id is required for export scope_type: group"
            )
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        return set(group.channel_ids)
    if not scope_id:
        raise ExportJobValidationError(
            f"scope_id is required for export scope_type: {scope_type}"
        )
    if scope_type == "sector":
        return {
            channel_id
            for channel_id, sector_id in org_index.channel_sector.items()
            if sector_id == scope_id
        }
    if scope_type == "company":
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
    if scope_id not in org_index.channel_company and scope_id not in org_index.channel_sector:
        raise KeyError(f"Channel not found: {scope_id}")


def _access_scope_from_export_scope(
    scope_type: str, scope_id: str | None
) -> AccessScope:
    if scope_type == "global":
        if scope_id is not None:
            raise ExportJobValidationError(
                "scope_id must be omitted for global exports"
            )
        return AccessScope.global_scope()
    if not scope_id:
        raise ExportJobValidationError(
            f"scope_id is required for export scope_type: {scope_type}"
        )
    if scope_type == "sector":
        return AccessScope.sector(scope_id)
    if scope_type == "company":
        return AccessScope.company(scope_id)
    if scope_type == "channel":
        return AccessScope.channel(scope_id)
    raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")


def _require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex,
) -> None:
    if not has_permission(user, permission, scope, org_index):
        _raise_missing_permission(permission)


def _raise_missing_permission(permission: Permission) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _has_any_export_permission(user: UserPrincipal) -> bool:
    if user.disabled:
        return False
    export_permissions = {
        Permission.EXPORT_ANALYTICS_REPORT,
        Permission.EXPORT_REVENUE_REPORT,
    }
    for grant in user.direct_permissions:
        if grant.active and grant.permission in export_permissions:
            return True
    for assignment in user.role_assignments:
        if (
            assignment.active
            and ROLE_PERMISSIONS.get(assignment.role, frozenset()) & export_permissions
        ):
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
    scan_truncated = (
        last_page_has_more and len(items) <= limit and scan_pages >= max_scan_pages
    )
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


def _previous_month(month: str) -> str:
    if not EXPORT_MONTH_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            f"export month must use YYYY-MM with a calendar month from 01 to 12: {month!r}"
        )
    year_value, month_value = month.split("-", maxsplit=1)
    year = int(year_value)
    month_number = int(month_value)
    if month_number == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"
