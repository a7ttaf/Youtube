"""FastAPI dependencies for authentication, authorization, and data sessions."""

import logging
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.principals import (
    PrincipalDataValidationError,
    PrincipalDisabledError,
    PrincipalLimitExceededError,
    PrincipalNotFoundError,
    PrincipalTransactionError,
    PrincipalValidationError,
    SqlAlchemyPrincipalLoader,
)
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, ScopeType
from ums_smart_revenue.config.settings import load_app_settings

logger = logging.getLogger(__name__)
DATABASE_PRINCIPAL_LOAD_ATTEMPTS = 2


def scope_from_header(scope_type: str, scope_id: str | None) -> AccessScope:
    """Parse gateway scope headers into a policy AccessScope."""
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
    """Build a bootstrap principal from trusted identity gateway headers."""
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
    """Fail closed until the app factory wires a concrete database session."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="database session not configured",
    )


def current_principal_from_database(
    session: Annotated[Session, Depends(current_db_session)],
    x_user_id: Annotated[str | None, Header()] = None,
    x_ums_trusted_gateway_token: Annotated[str | None, Header()] = None,
) -> UserPrincipal:
    """Load a request principal from SQL after trusted gateway validation."""
    normalized_user_id = x_user_id.strip() if x_user_id else None
    if not normalized_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    _require_trusted_gateway_token(x_ums_trusted_gateway_token)
    try:
        return _load_database_principal_with_retries(session, normalized_user_id)
    except PrincipalDisabledError as exc:
        logger.warning("Database principal lookup rejected disabled principal")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        ) from exc
    except PrincipalNotFoundError as exc:
        logger.warning("Database principal lookup rejected unknown principal")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        ) from exc
    except PrincipalLimitExceededError as exc:
        logger.warning("Database principal lookup rejected over-limit principal")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        ) from exc
    except PrincipalDataValidationError as exc:
        logger.error("Database principal lookup rejected corrupt stored data: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Principal authorization unavailable",
        ) from exc
    except PrincipalTransactionError as exc:
        logger.error("Database principal lookup rejected unsafe transaction: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Principal authorization unavailable",
        ) from exc
    except PrincipalValidationError as exc:
        logger.warning(
            "Database principal lookup rejected invalid principal input: %s", exc
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request",
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("Database principal lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Principal authorization unavailable",
        ) from exc
    except Exception as exc:
        logger.exception("Database principal lookup failed unexpectedly")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Principal authorization unavailable",
        ) from exc


def _load_database_principal_with_retries(
    session: Session,
    normalized_user_id: str,
) -> UserPrincipal:
    """Retry transient storage failures once before failing closed."""
    for attempt_index in range(DATABASE_PRINCIPAL_LOAD_ATTEMPTS):
        try:
            return SqlAlchemyPrincipalLoader(session).load(user_id=normalized_user_id)
        except SQLAlchemyError:
            if attempt_index + 1 >= DATABASE_PRINCIPAL_LOAD_ATTEMPTS:
                raise
            session.rollback()
            logger.warning("Retrying database principal lookup after storage failure")
    raise RuntimeError("database principal retry loop exhausted")


def _require_trusted_gateway_token(provided_token: str | None) -> None:
    """Require the configured trusted-gateway token to match the request token."""
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
