from fastapi import APIRouter, Depends

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.ui_metadata import permission_metadata, role_metadata

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/roles")
def list_roles(
    principal: UserPrincipal = Depends(current_principal_from_headers),
) -> list[dict[str, object]]:
    return role_metadata()


@router.get("/permissions")
def list_permissions(
    principal: UserPrincipal = Depends(current_principal_from_headers),
) -> list[dict[str, object]]:
    return permission_metadata()
