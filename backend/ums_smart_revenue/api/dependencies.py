"""FastAPI dependencies for authentication, authorization, and data sessions."""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)
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
from ums_smart_revenue.db.session import SessionFactory, begin_request_transaction
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import (
    TenantContextMissing,
    require_current_tenant,
)

logger = logging.getLogger(__name__)
DATABASE_PRINCIPAL_LOAD_ATTEMPTS = 2


@dataclass(frozen=True)
class TrustedGatewayIdentity:
    """Trusted gateway identity headers validated before database access."""

    user_id: str


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
    normalized_user_id = x_user_id.strip() if x_user_id else ""
    normalized_user_email = x_user_email.strip() if x_user_email else ""
    normalized_role = x_role.strip() if x_role else ""
    normalized_scope_type = x_scope_type.strip() if x_scope_type else ""
    if (
        not normalized_user_id
        or not normalized_user_email
        or not normalized_role
        or not normalized_scope_type
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    _require_trusted_gateway_token(x_ums_trusted_gateway_token)

    try:
        role = RoleKey(normalized_role)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role: {normalized_role}",
        ) from exc

    return UserPrincipal(
        user_id=normalized_user_id,
        email=normalized_user_email,
        role_assignments=(
            RoleAssignment(
                role=role,
                scope=scope_from_header(normalized_scope_type, x_scope_id),
            ),
        ),
        is_service_account=role == RoleKey.SYSTEM_INTEGRATION_USER,
    )


def current_db_session() -> Session:
    """Fail closed until the app factory wires a concrete database session."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="database session not configured",
    )


def current_platform_db_session() -> Session:
    """Fail closed until the app factory wires the privileged platform session."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="platform database session not configured",
    )


def current_trusted_gateway_identity(
    x_user_id: Annotated[str | None, Header()] = None,
    x_ums_trusted_gateway_token: Annotated[str | None, Header()] = None,
) -> TrustedGatewayIdentity:
    """Validate gateway headers and normalize the SQL principal id."""
    normalized_user_id = x_user_id.strip() if x_user_id else None
    if not normalized_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers",
        )
    _require_trusted_gateway_token(x_ums_trusted_gateway_token)
    try:
        normalized_user_id = str(UUID(normalized_user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request",
        ) from exc
    return TrustedGatewayIdentity(user_id=normalized_user_id)


def authenticated_session_dependency(
    session_factory: SessionFactory,
) -> Callable[..., Iterator[Session]]:
    """Build a database session dependency gated by gateway authentication."""

    def dependency(
        _identity: Annotated[TrustedGatewayIdentity, Depends(current_trusted_gateway_identity)],
    ) -> Iterator[Session]:
        """Open a database session only after gateway authentication succeeds."""
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return dependency


def current_principal_from_database(
    identity: Annotated[TrustedGatewayIdentity, Depends(current_trusted_gateway_identity)],
    session: Annotated[Session, Depends(current_db_session)],
) -> UserPrincipal:
    """Load a request principal from SQL after trusted gateway validation."""
    try:
        tenant = require_current_tenant()
        principal = _load_database_principal_with_retries(
            session, identity.user_id, tenant_id=str(tenant.id)
        )
        # FIX: The principal loader owns and closes its isolated read
        # transaction first. Only then can SQLite open the request write
        # envelope that keeps repository SAVEPOINTs subordinate to the final
        # dependency commit/rollback. PostgreSQL is a no-op here and retains
        # its normal implicit BEGIN plus RLS hook behavior.
        begin_request_transaction(session)
        return principal
    except TenantContextMissing as exc:
        logger.error("Database principal lookup missing tenant context")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Principal authorization unavailable",
        ) from exc
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
        logger.warning("Database principal lookup rejected invalid principal input: %s", exc)
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
    *,
    tenant_id: str,
) -> UserPrincipal:
    """Retry transient storage failures once before failing closed."""
    for attempt_index in range(DATABASE_PRINCIPAL_LOAD_ATTEMPTS):
        try:
            return SqlAlchemyPrincipalLoader(session).load(
                user_id=normalized_user_id,
                tenant_id=tenant_id,
            )
        except SQLAlchemyError as exc:
            if (
                attempt_index + 1 >= DATABASE_PRINCIPAL_LOAD_ATTEMPTS
                or not _is_retryable_principal_storage_error(exc)
            ):
                raise
            session.rollback()
            logger.warning("Retrying database principal lookup after storage failure")

    raise RuntimeError("database principal load did not return")


def _is_retryable_principal_storage_error(exc: SQLAlchemyError) -> bool:
    """Return whether a principal storage failure is safe to retry once."""
    if isinstance(exc, (DisconnectionError, OperationalError, SQLAlchemyTimeoutError)):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


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


# ============================================================================
# Purpose: Resolve a trusted tenant UUID from an already-authenticated
#   principal, falling back CLOSED to the bootstrap UMS tenant on a missing
#   or malformed tenant id (a bad header must not raise a bare ValueError
#   or grant a different tenant).
# Database/ORM: None; parses the principal's tenant_id string only.
# Standards: Shared by the connector and channel route modules — previously
#   two byte-identical private copies kept in step by comment (the
#   connectors -> channels import cycle that forced the replication was
#   removed by the api-layering follow-up). Fail-closed fallback is the
#   recorded contract; both callers depend on it.
# Blast Radius: Tenant attribution for connector-credential and channel
#   content-owner routes.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> credential-route
#     tenant attribution (previous owner of the original copy).
#   - File: backend/ums_smart_revenue/api/channels.py -> content-owner
#     write-path tenant attribution.
# ============================================================================
def resolve_tenant_uuid(user: UserPrincipal) -> UUID:
    """Resolve a trusted tenant UUID from the principal headers, falling back closed."""
    try:
        return UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)
    except ValueError:
        return UUID(UMS_TENANT_ID)
