"""Read-only Google source-rows API (spec §3).

Finance-gated, tenant-scoped, raw_payload never returned.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.source_rows_read import (
    MAX_SOURCE_ROW_PAGE_SIZE,
    SourceRowValidationError,
    get_source_row,
    list_source_rows,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

router = APIRouter(prefix="/revenue", tags=["revenue"])


# ============================================================================
# Purpose: Boundary permission gate for the read-only source-rows API.
# Database/ORM: None.
# Standards: Fail-closed 403 mirroring revenue-fact reads; raises typed HTTP.
# Blast Radius: Authorization read path only; no finance/audit/graph impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> same VIEW_REVENUE gate.
# ============================================================================
def _require_view_revenue(user: UserPrincipal) -> None:
    """Raise 403 unless the principal can view revenue (global scope)."""
    if not has_permission(user, Permission.VIEW_REVENUE, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_REVENUE.value}",
        )


def _tenant_uuid(user: UserPrincipal) -> UUID:
    """Resolve the principal's tenant UUID (fallback to UMS for pre-S2.4)."""
    return UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)


# ============================================================================
# Purpose: Return a tenant-scoped, newest-first page of Google source rows.
# Database/ORM: GoogleRevenueSourceRowORM via finance.source_rows_read.
# Standards: VIEW_REVENUE gate; SourceRowValidationError -> 422; raw_payload
#            never projected; keyset pagination envelope.
# Blast Radius: Read-only; no audit emission (mirrors connector-runs read).
# Connections:
#   - File: backend/ums_smart_revenue/finance/source_rows_read.py -> reader.
# ============================================================================
@router.get("/source-rows")
def list_revenue_source_rows(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    month: str,
    source_system: str | None = None,
    cursor_ingested_at: str | None = None,
    cursor_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SOURCE_ROW_PAGE_SIZE)] = 50,
) -> dict[str, object]:
    """Return a newest-first page of tenant-scoped Google source rows."""
    _require_view_revenue(user)
    try:
        page = list_source_rows(
            session,
            tenant_id=_tenant_uuid(user),
            month=month,
            source_system=source_system,
            cursor_ingested_at=cursor_ingested_at,
            cursor_id=cursor_id,
            limit=limit,
        )
    except SourceRowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    items = [e.to_api() for e in page.items]
    return {
        "items": items,
        "pagination": {
            "limit": page.limit,
            "returned": len(items),
            "has_more": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
        },
    }


# ============================================================================
# Purpose: Return one tenant-scoped source row by id.
# Database/ORM: GoogleRevenueSourceRowORM via finance.source_rows_read.
# Standards: VIEW_REVENUE gate; invalid id -> 422; absent/cross-tenant -> 404
#            (no existence leak); raw_payload never projected.
# Blast Radius: Read-only; no audit emission.
# Connections:
#   - File: backend/ums_smart_revenue/finance/source_rows_read.py -> reader.
# ============================================================================
@router.get("/source-rows/{row_id}")
def get_revenue_source_row(
    row_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
) -> dict[str, object]:
    """Return one tenant-scoped source row; 404 if absent or cross-tenant."""
    _require_view_revenue(user)
    try:
        entry = get_source_row(
            session, tenant_id=_tenant_uuid(user), row_id=row_id
        )
    except SourceRowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source row not found"
        )
    return entry.to_api()
