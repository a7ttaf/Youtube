"""Smart revenue reconciliation API routes.

Thin FastAPI surface for the month-level reconciliation workflow: a POST that
computes + persists per-channel reductions (delegating to
ReconciliationWorkflowService), and a GET that reads the persisted
reconciliation explanation for one channel-month. All business logic lives in
the finance service/repository layers; routes only parse input, enforce the
boundary permission gate, translate typed errors, and serialize the response.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.authz import require_permission
from ums_smart_revenue.api.channels import audit_record_to_api
from ums_smart_revenue.api.dependencies import (
    current_platform_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.dependencies_finance import (
    current_org_access_index,
    current_revenue_audit_sink,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.finance.decimal_formatting import (
    decimal_to_api as _decimal_to_api,
)
from ums_smart_revenue.finance.explanations import (
    REVENUE_RECONCILIATION_METRIC,
    NumberExplanationEntry,
    SqlAlchemyNumberExplanationRepository,
)
from ums_smart_revenue.finance.reconciliation_service import (
    MonthLockedError,
    ReconciliationWorkflowService,
)
from ums_smart_revenue.finance.reconciliation_workflow import (
    ChannelReconciliation,
    MonthReconciliationResult,
)

router = APIRouter(prefix="/revenue", tags=["revenue"])
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ReconcileMonthRequest(BaseModel):
    """Request body for a month reconciliation run."""

    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        """Trim and reject blank reconciliation audit reasons."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


def _require_valid_month(month: str) -> None:
    """Raise HTTP 422 for a malformed YYYY-MM month path segment."""
    if not _MONTH_PATTERN.fullmatch(month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )


def _channel_line_to_api(line: ChannelReconciliation) -> dict[str, object]:
    """Serialize one per-channel reconciliation line for the API response."""
    return {
        "youtube_channel_id": line.youtube_channel_id,
        "gross_usd": _decimal_to_api(line.gross_usd),
        "us_tax_usd": _decimal_to_api(line.us_tax_usd),
        "yt_adsense_fee_usd": _decimal_to_api(line.yt_adsense_fee_usd),
        "adsense_bank_fee_usd": _decimal_to_api(line.adsense_bank_fee_usd),
        "fx_variance_usd": _decimal_to_api(line.fx_variance_usd),
        "net_received_usd": _decimal_to_api(line.net_received_usd),
        "us_view_share": (
            None if line.us_view_share is None else _decimal_to_api(line.us_view_share)
        ),
    }


def _result_to_api(result: MonthReconciliationResult) -> dict[str, object]:
    """Serialize the whole-month reconciliation result into the API shape."""
    return {
        "month": result.month,
        "channels": [_channel_line_to_api(line) for line in result.channels],
        "totals": {
            "gross_total_usd": _decimal_to_api(result.gross_total_usd),
            "us_tax_total_usd": _decimal_to_api(result.us_tax_total_usd),
            "yt_adsense_fee_total_usd": _decimal_to_api(result.yt_adsense_fee_total_usd),
            "adsense_bank_fee_total_usd": _decimal_to_api(result.adsense_bank_fee_total_usd),
            "fx_total_usd": _decimal_to_api(result.fx_total_usd),
            "net_total_usd": _decimal_to_api(result.net_total_usd),
            "yt_adsense_fee_pct": (
                None
                if result.yt_adsense_fee_pct is None
                else _decimal_to_api(result.yt_adsense_fee_pct)
            ),
        },
        "warnings": list(result.warnings),
    }


@router.post("/months/{month}/reconcile")
def reconcile_month(
    month: str,
    payload: ReconcileMonthRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Compute and persist a month's smart revenue reconciliation, then audit it."""
    # ========================================================================
    # Purpose: Boundary for the month reconciliation write/compute. Validates the
    #   month, gates the caller, delegates the gather->compute->persist->audit
    #   work to ReconciliationWorkflowService, and serializes the result.
    # Database/ORM: None directly; the service reads facts/adsense/bank and writes
    #   deduction_components + number_explanations (+ ALLOCATION facts).
    # Standards: Thin route; fail-closed gates before any data access; typed
    #   MonthLockedError -> 409; logic owned by the finance service.
    # Blast Radius: Authorization, finance source-of-truth writes, one audit event.
    #   Gate requires CHANGE_ALLOCATION_RULE plus finance read privileges because
    #   the response exposes channel totals, finalized payment-derived deltas,
    #   bank reconciliation deltas, confidence/explanation warnings, and may
    #   write ALLOCATION facts.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/reconciliation_service.py
    #   - File: Docs/superpowers/plans/2026-06-09-track-f-smart-reconciliation.md
    # ========================================================================
    _require_valid_month(month)
    require_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(month))
    require_permission(user, Permission.VIEW_REVENUE, AccessScope.global_scope())
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
    require_permission(
        user,
        Permission.VIEW_BANK_RECONCILIATION,
        AccessScope.finance_month(month),
    )
    require_permission(user, Permission.VIEW_CONFIDENCE, AccessScope.global_scope())
    service = ReconciliationWorkflowService(session, audit_sink=audit_sink)
    try:
        result = service.run(month=month, actor=user, reason=payload.reason)
    except MonthLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _result_to_api(result)


@router.get("/channels/{channel_id}/months/{month}/reconciliation")
def get_channel_month_reconciliation(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    session: Annotated[Session, Depends(current_platform_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    """Return the persisted reconciliation explanation for a channel-month."""
    # ========================================================================
    # Purpose: Read boundary for a channel-month reconciliation explanation.
    #   Gates VIEW_REVENUE + VIEW_CONFIDENCE at channel scope,
    #   VIEW_FINALIZED_PAYMENTS + VIEW_BANK_RECONCILIATION at finance-month
    #   scope, and VIEW_CONFIDENCE at channel scope before returning the
    #   persisted revenue_reconciliation_usd explanation (404 when none
    #   exists). When the persisted explanation contains bank-derived
    #   components, also records a BANK_RECONCILIATION_VIEWED event.
    # Database/ORM: reads number_explanations via the explanation repository;
    #   appends REVENUE_VIEWED, PAYMENT_VIEWED, and (when applicable)
    #   BANK_RECONCILIATION_VIEWED rows to audit_logs.
    # Standards: Thin route; fail-closed gate before any data access; missing
    #   explanation -> 404; audit details avoid component/payment payloads.
    # Blast Radius: Authorization (read), finance explanation read, audit writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/explanations.py -> get method.
    #   - File: backend/ums_smart_revenue/auth/audit_service.py -> audit sink.
    # ========================================================================
    _require_valid_month(month)
    target_scope = AccessScope.channel(channel_id)
    require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
    require_permission(
        user,
        Permission.VIEW_BANK_RECONCILIATION,
        AccessScope.finance_month(month),
    )
    repository = SqlAlchemyNumberExplanationRepository(session)
    explanation = repository.get_explanation(
        month=month,
        entity_type="channel",
        entity_id=channel_id,
        metric=REVENUE_RECONCILIATION_METRIC,
    )
    if explanation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"No reconciliation explanation for {channel_id} in {month}"),
        )
    details = {
        "month": month,
        "channel_id": channel_id,
        "metric": REVENUE_RECONCILIATION_METRIC,
    }
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="channel_reconciliation_explanation",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details=details,
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="channel_reconciliation_explanation",
        entity_id=f"{channel_id}:{month}",
        scope=AccessScope.finance_month(month),
        details=details,
    )
    audit_events = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    if _explanation_includes_bank_components(explanation):
        bank_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
            entity_type="channel_reconciliation_explanation",
            entity_id=f"{channel_id}:{month}",
            scope=AccessScope.finance_month(month),
            details=details,
        )
        audit_events.append(audit_record_to_api(bank_record))
    response = explanation.to_api()
    response["audit_events"] = audit_events
    return response


def _explanation_includes_bank_components(
    explanation: NumberExplanationEntry,
) -> bool:
    """Return True when the explanation surfaces bank-derived hop-3 components."""
    bank_keys = {"adsense_bank_fee_usd", "fx_variance_usd"}
    for component in explanation.components:
        if component.get("key") in bank_keys:
            value = component.get("value")
            if value is not None and str(value) not in {"", "0", "0.000000"}:
                return True
    return False
