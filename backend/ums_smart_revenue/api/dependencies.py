from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, ScopeType


def scope_from_header(scope_type: str, scope_id: str | None) -> AccessScope:
    try:
        parsed_scope_type = ScopeType(scope_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown scope type: {scope_type}",
        ) from exc
    return AccessScope(parsed_scope_type, scope_id)


def current_principal_from_headers(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_email: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
    x_scope_type: Annotated[str | None, Header()] = None,
    x_scope_id: Annotated[str | None, Header()] = None,
) -> UserPrincipal:
    if not all([x_user_id, x_user_email, x_role, x_scope_type]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )

    try:
        role = RoleKey(x_role)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown role: {x_role}") from exc

    return UserPrincipal(
        user_id=x_user_id,
        email=x_user_email,
        role_assignments=[
            RoleAssignment(
                role=role,
                scope=scope_from_header(x_scope_type, x_scope_id),
            )
        ],
        is_service_account=role == RoleKey.SYSTEM_INTEGRATION_USER,
    )


def current_db_session() -> Session:
    raise RuntimeError("Database session dependency has not been configured")
