"""Read-only org-unit listing route for the Registry view."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.org.org_units_read import SqlAlchemyOrgUnitReader

router = APIRouter(prefix="/org-units", tags=["org-units"])


def current_org_unit_reader(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyOrgUnitReader:
    """Build the org-unit reader bound to the current database session."""
    return SqlAlchemyOrgUnitReader(session)


# ============================================================================
# Purpose: List active org units (HOLDING/SECTOR/COMPANY) for the current
#   tenant so the Registry view can label channels by company and sector.
# Database/ORM: OrgUnitORM (org_units), via SqlAlchemyOrgUnitReader. Read-only.
# Standards: Thin route — boundary VIEW_ANALYTICS gate, then a service read,
#   then typed serialization. No writes, no audit event (read-only structure
#   metadata, consistent with GET /channels which emits none).
# Blast Radius: Authorization (fail-closed VIEW_ANALYTICS gate). No finance,
#   audit, Neo4j, or export impact. Names are the same audience as the channel
#   list; scoped analytics viewers get the full active list because IDs were
#   already visible via GET /channels and names only label them.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py ->
#       _require_analytics_view_permission (the gate semantics mirrored below).
#   - File: backend/ums_smart_revenue/org/org_units_read.py -> the reader.
# ============================================================================
@router.get("")
def list_org_units(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    reader: Annotated[SqlAlchemyOrgUnitReader, Depends(current_org_unit_reader)],
) -> list[dict[str, object]]:
    """Return active current-tenant org units; 403 without VIEW_ANALYTICS."""
    _require_analytics_view_permission(user)
    return [entry.to_api() for entry in reader.list_active_units()]


def _require_analytics_view_permission(user: UserPrincipal) -> None:
    """Raise HTTP 403 if the principal lacks any VIEW_ANALYTICS grant.

    Fail-closed mirror of api/channels.py:_require_analytics_view_permission.
    A disabled principal or one without any granted VIEW_ANALYTICS scope must
    403, never receive a silent empty list that reads as "no org units exist".

    Raises:
        HTTPException: HTTP 403 when the principal is disabled or holds no
            VIEW_ANALYTICS grant in any scope.
    """
    if user.disabled or not _granted_scopes_for_permission(
        user, Permission.VIEW_ANALYTICS
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_ANALYTICS.value}",
        )


def _direct_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> list[AccessScope]:
    """Yield active direct scopes granting the permission."""
    return [
        grant.scope
        for grant in user.direct_permissions
        if grant.active and grant.permission == permission
    ]


def _role_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> list[AccessScope]:
    """Yield active role-derived scopes granting the permission."""
    return [
        assignment.scope
        for assignment in user.role_assignments
        if assignment.active
        and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset())
    ]


def _granted_scopes_for_permission(
    user: UserPrincipal, permission: Permission
) -> tuple[AccessScope, ...]:
    """Return all AccessScope values for which the principal holds a permission.

    Mirrors the channels-route helper: both active direct grants and active
    role assignments carrying the permission are included. Returns an empty
    tuple if no grant exists (falsy → caller should 403).
    """
    return tuple(
        _direct_scopes_for_permission(user, permission)
        + _role_scopes_for_permission(user, permission)
    )
