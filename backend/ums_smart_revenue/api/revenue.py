from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.org.access_index import load_org_access_index_from_session


router = APIRouter(prefix="/revenue", tags=["revenue"])


class AuthorizationCheckResponse(BaseModel):
    authorized: bool
    channel_id: str
    permission: str


def current_org_access_index(
    session: Annotated[Session, Depends(current_db_session)],
) -> OrgAccessIndex:
    return load_org_access_index_from_session(session)


@router.get("/channels/{channel_id}/authorization-check", response_model=AuthorizationCheckResponse)
def check_channel_revenue_authorization(
    channel_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> AuthorizationCheckResponse:
    target_scope = AccessScope.channel(channel_id)
    if not has_permission(user, Permission.VIEW_REVENUE, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_REVENUE.value}",
        )
    return AuthorizationCheckResponse(
        authorized=True,
        channel_id=channel_id,
        permission=Permission.VIEW_REVENUE.value,
    )
