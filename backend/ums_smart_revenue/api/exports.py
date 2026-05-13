from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.groups import current_group_registry
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
    ExportJobNotFoundError,
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


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def request_export(
    payload: ExportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(current_group_registry)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    try:
        _require_export_permissions(
            user=user,
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            org_index=org_index,
            group_registry=group_registry,
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
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_JOB_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    try:
        page = repository.list_jobs(
            requested_by=user.user_id, limit=limit, offset=offset
        )
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return {
        "items": [item.to_api() for item in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    }


@router.get("/{export_id}")
def get_export(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(current_group_registry)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
) -> dict[str, object]:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id)
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if export_job.requested_by != user.user_id:
        _require_export_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
    return export_job.to_api()


@router.get("/{export_id}/finance-workbook-preview")
def preview_finance_workbook(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(current_group_registry)
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
        export_job = repository.get_job(export_id)
        if export_job.export_type != "FINANCE_EXCEL":
            raise FinanceWorkbookPreviewValidationError(
                "finance workbook preview only supports FINANCE_EXCEL exports"
            )
        _require_finance_export_artifact_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
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
        ChannelGroupRegistryStore, Depends(current_group_registry)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id)
        if export_job.export_type != "FINANCE_EXCEL":
            raise FinanceWorkbookPreviewValidationError(
                "finance workbook download only supports FINANCE_EXCEL exports"
            )
        _require_finance_export_artifact_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
        )
        preview = _build_finance_workbook_preview_for_export(
            export_job=export_job,
            session=session,
            org_index=org_index,
            group_registry=group_registry,
        )
        workbook_bytes = build_finance_workbook_xlsx(preview)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
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
        artifact_type="finance_workbook_xlsx",
        include_download_event=True,
    )
    filename = f"ums-finance-{export_job.month}-{export_job.scope_type}.xlsx"
    return Response(
        content=workbook_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{export_id}/executive.pdf")
def download_executive_pdf(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(current_group_registry)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id)
        if export_job.export_type != "EXECUTIVE_PDF":
            raise ExecutivePdfValidationError(
                "executive PDF download only supports EXECUTIVE_PDF exports"
            )
        _require_finance_export_artifact_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
        )
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
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
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
        artifact_type="executive_pdf",
        include_download_event=True,
    )
    filename = f"ums-executive-{export_job.month}-{export_job.scope_type}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{export_id}/branded-slide-pack.pptx")
def download_branded_slide_pack(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[
        ChannelGroupRegistryStore, Depends(current_group_registry)
    ],
    repository: Annotated[
        SqlAlchemyExportJobRepository, Depends(current_export_job_repository)
    ],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> Response:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id)
        if export_job.export_type != "BRANDED_SLIDE_PACK":
            raise BrandedSlidePackValidationError(
                "branded slide pack download only supports BRANDED_SLIDE_PACK exports"
            )
        _require_finance_export_artifact_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            month=export_job.month,
            org_index=org_index,
            group_registry=group_registry,
        )
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
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")
        ) from exc
    except ExportJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
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
        artifact_type="branded_slide_pack_pptx",
        include_download_event=True,
    )
    filename = f"ums-branded-{export_job.month}-{export_job.scope_type}.pptx"
    return Response(
        content=pptx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
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


def _build_finance_source_summaries_for_export(
    *,
    export_job,
    session: Session,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> _FinanceExportSourceSummaries:
    channel_ids = _channel_ids_for_export_scope(
        scope_type=export_job.scope_type,
        scope_id=export_job.scope_id,
        org_index=org_index,
        group_registry=group_registry,
    )
    facts = SqlAlchemyRevenueFactRepository(session).list_month_facts(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    manual_overrides = SqlAlchemyManualOverrideRepository(session).list_month_overrides(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
    )
    payments = SqlAlchemyAdSensePaymentRepository(session).list_month_payments(
        month=export_job.month
    )
    bank_entries = SqlAlchemyBankReconciliationRepository(session).list_month_entries(
        month=export_job.month
    )
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
    artifact_type: str,
    include_download_event: bool,
):
    revenue_scope = _access_scope_from_export_scope(
        export_job.scope_type,
        export_job.scope_id,
    )
    month_scope = AccessScope.finance_month(export_job.month)
    details = {
        "export_type": export_job.export_type,
        "artifact_type": artifact_type,
        "month": export_job.month,
    }
    audit_records = [
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="export_job",
            entity_id=export_job.id,
            scope=revenue_scope,
            details=details,
        ),
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


def _require_export_permissions(
    *,
    user: UserPrincipal,
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> None:
    finance_export = is_finance_export_type(export_type)
    export_permission = (
        Permission.EXPORT_REVENUE_REPORT
        if finance_export
        else Permission.EXPORT_ANALYTICS_REPORT
    )
    view_permission = (
        Permission.VIEW_REVENUE if finance_export else Permission.VIEW_ANALYTICS
    )

    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError(
                "scope_id is required for export scope_type: group"
            )
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        if not group.channel_ids:
            raise ExportJobValidationError("group exports require at least one channel")
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
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    month: str,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> None:
    _require_export_permissions(
        user=user,
        export_type=export_type,
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
        group_registry=group_registry,
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
        return {scope_id}
    raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")


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
