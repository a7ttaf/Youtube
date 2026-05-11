import logging
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.principals import (
    PrincipalDisabledError,
    PrincipalNotFoundError,
    PrincipalValidationError,
    SqlAlchemyPrincipalLoader,
)
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, ScopeType
from ums_smart_revenue.config.settings import load_app_settings

logger = logging.getLogger(__name__)


def scope_from_header(scope_type: str, scope_id: str | None) -> AccessScope:
    try:
        parsed_scope_type = ScopeType(scope_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown scope type: {scope_type}",
        ) from exc
    normalized_scope_id = scope_id.strip() if scope_id else None
    if parsed_scope_type == ScopeType.GLOBAL:
        if normalized_scope_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scope_id must be omitted for global scope",
            )
    elif normalized_scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope_id is required for scope type: {parsed_scope_type.value}",
        )
    return AccessScope(parsed_scope_type, normalized_scope_id)


def current_principal_from_headers(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_email: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
    x_scope_type: Annotated[str | None, Header()] = None,
    x_scope_id: Annotated[str | None, Header()] = None,
    x_ums_trusted_gateway_token: Annotated[str | None, Header()] = None,
) -> UserPrincipal:
    if not all([x_user_id, x_user_email, x_role, x_scope_type]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    _require_trusted_gateway_token(x_ums_trusted_gateway_token)

    try:
        role = RoleKey(x_role)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role: {x_role}",
        ) from exc

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
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="database session not configured",
    )


def current_principal_from_database(
    session: Annotated[Session, Depends(current_db_session)],
    x_user_id: Annotated[str | None, Header()] = None,
    x_ums_trusted_gateway_token: Annotated[str | None, Header()] = None,
) -> UserPrincipal:
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    _require_trusted_gateway_token(x_ums_trusted_gateway_token)
    try:
        return SqlAlchemyPrincipalLoader(session).load(user_id=x_user_id)
    except PrincipalDisabledError as exc:
        logger.warning(
            "Database principal lookup rejected disabled principal",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        ) from exc
    except PrincipalNotFoundError as exc:
        logger.warning(
            "Database principal lookup rejected unknown principal",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        ) from exc
    except PrincipalValidationError as exc:
        logger.warning(
            "Database principal lookup rejected invalid principal input",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request",
        ) from exc


def _require_trusted_gateway_token(provided_token: str | None) -> None:
    configured_token = load_app_settings().trusted_gateway_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trusted gateway authentication is not configured",
        )
    if not provided_token or not compare_digest(provided_token, configured_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid trusted gateway token",
        )
