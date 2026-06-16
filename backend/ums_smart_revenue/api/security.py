from fastapi import APIRouter, Depends, HTTPException, status

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.ui_metadata import permission_metadata, role_metadata

router = APIRouter(prefix="/security", tags=["security"])


# ============================================================================
# Purpose: Fail closed on the security-metadata catalog endpoints. The role and
#   permission catalogs expose the full authorization model plus each action's
#   sensitive / auditOnUse flags, so they are restricted to audit-permissioned
#   principals rather than every authenticated caller.
# Database/ORM: None (reads static UI metadata, no model access).
# Standards: Boundary permission check via the shared has_permission policy at
#   global scope; raises HTTP 403 on denial. Fail-closed.
# Blast Radius: Authorization (read disclosure of the authz model). No finance,
#   audit-write, or graph-projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/auth/ui_metadata.py -> catalog source.
#   - File: Docs/security/ROLE_PERMISSION_MODEL.md -> security metadata is
#     auditor-scoped; VIEW_AUDIT_LOG is the auditor read permission.
# ============================================================================
def _require_audit_view(principal: UserPrincipal) -> None:
    """Raise HTTP 403 unless the caller holds VIEW_AUDIT_LOG at global scope."""
    if not has_permission(principal, Permission.VIEW_AUDIT_LOG, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_AUDIT_LOG.value}",
        )


@router.get("/roles")
def list_roles(
    principal: UserPrincipal = Depends(current_principal_from_headers),
) -> list[dict[str, object]]:
    _require_audit_view(principal)
    return role_metadata()


@router.get("/permissions")
def list_permissions(
    principal: UserPrincipal = Depends(current_principal_from_headers),
) -> list[dict[str, object]]:
    _require_audit_view(principal)
    return permission_metadata()
