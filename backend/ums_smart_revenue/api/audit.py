import json
from csv import DictWriter
from datetime import datetime
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_log import (
    MAX_AUDIT_LOG_PAGE_SIZE,
    AuditLogValidationError,
    SqlAlchemyAuditLogRepository,
)
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope

router = APIRouter(prefix="/audit", tags=["audit"])

# Hard cap on rows in a single synchronous CSV export. When exceeded the
# response carries X-Truncated: true so callers never silently lose audit rows.
AUDIT_EXPORT_MAX_ROWS = 10_000

# Upper bound on the GET /audit/summary ``window_hours`` query parameter.
# 8760 = 365 * 24, the maximum window a caller can ask for (one full year).
# The repository's count_summary accepts a (created_at >= now - window_hours)
# filter, so 8760 keeps the most-recent-count query bounded and predictable.
MAX_SUMMARY_WINDOW_HOURS = 8760

# Characters that trigger CSV/Excel formula injection when they lead a cell.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class AuditSummaryResponse(BaseModel):
    """Aggregate audit counts surfaced by the GET /audit/summary tiles."""

    total_events: int
    sensitive_events: int
    recent_count: int
    window_hours: int


def current_audit_log_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAuditLogRepository:
    """Return the SQL audit-log repository bound to the current session."""
    return SqlAlchemyAuditLogRepository(session)


@router.get("/events")
def list_audit_events(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAuditLogRepository, Depends(current_audit_log_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    cursor_created_at: datetime | None = None,
    cursor_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LOG_PAGE_SIZE)] = 50,
) -> dict[str, object]:
    """Return the filtered audit-event page and the self-audit record."""
    audit_scope = AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    include_sensitive_details = has_permission(
        user, Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS, audit_scope
    )
    try:
        page = repository.list_events(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            exclude_event_type=AuditEventType.AUDIT_LOG_VIEWED.value,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            limit=limit,
        )
    except AuditLogValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    details_redacted = not include_sensitive_details and any(
        item.sensitive for item in page.items
    )
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.AUDIT_LOG_VIEWED,
        entity_type="audit_log_page",
        entity_id=event_type or "all",
        scope=audit_scope,
        details={
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "returned": len(page.items),
            "details_redacted": details_redacted,
        },
    )
    return {
        "items": [
            item.to_api(include_sensitive_details=include_sensitive_details)
            for item in page.items
        ],
        "pagination": {
            "limit": page.limit,
            "returned": len(page.items),
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        },
        "audit_event": audit_record_to_api(record),
    }


@router.get("/summary")
def get_audit_summary(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAuditLogRepository, Depends(current_audit_log_repository)
    ],
    window_hours: Annotated[int, Query(ge=1, le=MAX_SUMMARY_WINDOW_HOURS)] = 24,
) -> AuditSummaryResponse:
    """Return tenant-scoped aggregate audit counts for the summary tiles."""
    # ========================================================================
    # Purpose: Serve the Audit screen's summary tiles (total / sensitive /
    #   recent counts) from a single tenant-scoped aggregate query. Counts
    #   only — no per-row payload — so it is redaction-safe for a plain
    #   audit viewer.
    # Database/ORM: SqlAlchemyAuditLogRepository.count_summary over
    #   AuditLogORM (read-only COUNT aggregates; no write).
    # Standards: Same fail-closed VIEW_AUDIT_LOG@global gate as /events;
    #   thin route (gate -> repo -> shape); typed AuditLogValidationError ->
    #   HTTPException(422) at the boundary. No self-audit (no AUDIT_LOG_VIEWED
    #   row) so the aggregate cannot pollute its own counts.
    # Blast Radius: Audit read-only aggregate; no write, no schema change, no
    #   migration; same fail-closed gate as /events. No graph/finance impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/audit_log.py -> count_summary.
    #   - File: backend/ums_smart_revenue/api/audit.py -> list_audit_events
    #     gate parity.
    # ========================================================================
    audit_scope = AccessScope.global_scope()
    # Fail-closed permission check; counts disclose no payload, so the
    # sensitive-payload gate is intentionally NOT required here.
    _require_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    try:
        counts = repository.count_summary(window_hours=window_hours)
    except AuditLogValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return AuditSummaryResponse(
        total_events=counts.total_events,
        sensitive_events=counts.sensitive_events,
        recent_count=counts.recent_count,
        window_hours=window_hours,
    )


@router.get("/events/export")
def export_audit_events(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAuditLogRepository, Depends(current_audit_log_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> Response:
    """Export the current filtered audit slice as a CSV attachment."""
    # ========================================================================
    # Purpose: Export the current filtered audit-event slice as a CSV
    #   attachment for dashboard reporting. Always starts from newest-first;
    #   never consumes the caller's loaded-page cursor state.
    # Database/ORM: SqlAlchemyAuditLogRepository (same read model as
    #   /audit/events); emits one EXPORT_DOWNLOADED audit row via AuditSink.
    # Standards: Same fail-closed permission gate and redaction rule as the
    #   list route; typed AuditLogValidationError -> HTTPException at boundary.
    # Blast Radius: Audit read + one audit self-event; no finance/auth change.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/audit.py -> list_audit_events
    #     gate/filter parity.
    #   - File: Docs/12_BACKEND_API_SPEC.md -> CSV export contract.
    # ========================================================================
    audit_scope = AccessScope.global_scope()
    # Fail-closed permission check BEFORE any audit emission or row gathering.
    _require_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    include_sensitive_details = has_permission(
        user, Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS, audit_scope
    )

    # ------------------------------------------------------------------------
    # Snapshot-before-audit: materialize ALL export rows by internal cursor
    # iteration BEFORE emitting the download event, so the CSV can never
    # contain its own EXPORT_DOWNLOADED row. Cap at AUDIT_EXPORT_MAX_ROWS.
    # ------------------------------------------------------------------------
    items: list = []
    cursor_created_at: datetime | None = None
    cursor_id: str | None = None
    truncated = False
    try:
        while len(items) < AUDIT_EXPORT_MAX_ROWS:
            page = repository.list_events(
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                exclude_event_type=AuditEventType.AUDIT_LOG_VIEWED.value,
                cursor_created_at=cursor_created_at,
                cursor_id=cursor_id,
                limit=MAX_AUDIT_LOG_PAGE_SIZE,
            )
            items.extend(page.items)
            if page.next_cursor is None:
                break
            cursor_created_at = datetime.fromisoformat(page.next_cursor["created_at"])
            cursor_id = page.next_cursor["id"]
        else:
            # Loop exited via the cap; rows may remain beyond it.
            truncated = page.next_cursor is not None
    except AuditLogValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if len(items) > AUDIT_EXPORT_MAX_ROWS:
        truncated = True
        items = items[:AUDIT_EXPORT_MAX_ROWS]

    rows = [
        item.to_api(include_sensitive_details=include_sensitive_details)
        for item in items
    ]
    csv_text = _audit_events_to_csv(rows)

    details_redacted = not include_sensitive_details and any(
        item.sensitive for item in items
    )
    # One download event, emitted AFTER the snapshot is materialized.
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_DOWNLOADED,
        entity_type="audit_events_export",
        entity_id=event_type or "all",
        scope=audit_scope,
        permission_override=Permission.VIEW_AUDIT_LOG,
        details={
            "filters": {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
            },
            "returned": len(rows),
            "truncated": truncated,
            "details_redacted": details_redacted,
        },
    )

    headers = {"Content-Disposition": 'attachment; filename="audit-events.csv"'}
    if truncated:
        headers["X-Truncated"] = "true"
    return Response(content=csv_text, media_type="text/csv", headers=headers)


def _require_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise HTTP 403 when the caller lacks the requested permission."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _csv_safe(value: str) -> str:
    """Prefix risky leading characters so CSV consumers do not execute cells."""
    # ========================================================================
    # Purpose: Neutralize CSV/Excel formula injection by prefixing an
    #   apostrophe to any string cell beginning with =, +, -, @, tab, or CR.
    # Database/ORM: None.
    # Standards: Defense-in-depth for operator-opened spreadsheets.
    # Blast Radius: Export serialization only.
    # ========================================================================
    if value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


def _bool_cell(value: object) -> str:
    """Render truthy values as lowercase CSV booleans."""
    return "true" if value else "false"


def _audit_events_to_csv(rows: list[dict[str, object]]) -> str:
    """Serialize already-redacted audit rows into deterministic CSV text."""
    # ========================================================================
    # Purpose: Render to_api()-serialized audit rows to a deterministic CSV.
    #   Stable column order, ISO created_at passthrough, lowercase booleans,
    #   stable-JSON details, and "" for redacted details.
    # Database/ORM: None (accepts already-serialized + already-redacted rows).
    # Standards: Redaction contract enforced upstream by to_api; this layer
    #   only formats and applies the formula-injection guard.
    # Blast Radius: Export serialization only.
    # ========================================================================
    fieldnames = [
        "created_at",
        "event_type",
        "user_id",
        "entity_type",
        "entity_id",
        "scope_type",
        "scope_id",
        "request_id",
        "reason",
        "sensitive",
        "details_redacted",
        "details",
    ]
    buffer = StringIO()
    writer = DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        details_redacted = bool(row["details_redacted"])
        if details_redacted:
            details_cell = ""
        else:
            details_cell = json.dumps(
                row["details"], separators=(",", ":"), sort_keys=True
            )
        writer.writerow(
            {
                # created_at is already an ISO-8601 string from to_api.
                "created_at": _csv_safe(str(row["created_at"])),
                "event_type": _csv_safe(str(row["event_type"])),
                "user_id": _csv_safe(str(row.get("user_id") or "")),
                "entity_type": _csv_safe(str(row.get("entity_type") or "")),
                "entity_id": _csv_safe(str(row.get("entity_id") or "")),
                "scope_type": _csv_safe(str(row.get("scope_type") or "")),
                "scope_id": _csv_safe(str(row.get("scope_id") or "")),
                "request_id": _csv_safe(str(row.get("request_id") or "")),
                "reason": _csv_safe(str(row.get("reason") or "")),
                "sensitive": _bool_cell(row["sensitive"]),
                "details_redacted": _bool_cell(details_redacted),
                "details": _csv_safe(details_cell),
            }
        )
    return buffer.getvalue()
