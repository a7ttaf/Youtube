"""Database-backed principal loading for request authorization."""

from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import PermissionGrant, RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, ScopeType
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)

MAX_ACTIVE_ROLE_ASSIGNMENTS = 256
MAX_ACTIVE_PERMISSION_GRANTS = 512
PRINCIPAL_QUERY_TIMEOUT_MS = 5_000


class UserStatus(StrEnum):
    """Allowed stored user status values for principal loading."""

    ACTIVE = "active"
    DISABLED = "disabled"
    SERVICE = "service"


class PrincipalLoadError(ValueError):
    """Base error for database-backed principal loading failures."""

    pass


class PrincipalNotFoundError(PrincipalLoadError):
    """Raised when the trusted user id does not map to a stored user."""

    pass


class PrincipalDisabledError(PrincipalLoadError):
    """Raised when the stored user exists but is not allowed to authenticate."""

    pass


class PrincipalValidationError(PrincipalLoadError):
    """Raised when request principal input cannot be parsed."""

    pass


class PrincipalDataValidationError(PrincipalLoadError):
    """Raised when stored principal data cannot be parsed into policy objects."""

    pass


class PrincipalTransactionError(PrincipalLoadError):
    """Raised when a caller transaction prevents read isolation setup."""

    pass


class PrincipalLimitExceededError(PrincipalLoadError):
    """Raised when a principal exceeds request-time role or grant limits."""

    pass


class SqlAlchemyPrincipalLoader:
    """Load authorization principals from SQL state instead of identity headers."""

    def __init__(self, session: Session):
        """Store the SQLAlchemy session used for one principal lookup."""
        self._session = session

    def load(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
    ) -> UserPrincipal:
        """Return a UserPrincipal using a loader-owned isolated transaction.

        ``tenant_id`` is normalized to a canonical UUID string before it
        reaches policy code. Blank values are treated as missing during
        the pre-S2.4 window; malformed non-empty values fail closed.
        """
        parsed_user_id = _parse_uuid(user_id)
        parsed_tenant_id = _parse_tenant_uuid(tenant_id)
        if self._session.in_transaction():
            raise PrincipalTransactionError(
                "Principal loads require a session without an active transaction"
            )
        with self._session.begin():
            self._prepare_read_connection()
            return self._load_principal(parsed_user_id, tenant_id=parsed_tenant_id)

    def _load_principal(
        self,
        parsed_user_id: UUID,
        *,
        tenant_id: str | None,
    ) -> UserPrincipal:
        """Load the principal rows within the current transaction scope."""
        user = self._session.get(UserORM, parsed_user_id)
        if user is None:
            raise PrincipalNotFoundError("User is not registered")
        user_status = _parse_user_status(user.status)
        if user_status == UserStatus.DISABLED:
            raise PrincipalDisabledError("User is disabled")
        # FIX: Bind the principal to the stored user's tenant, not a caller-
        # supplied tenant value that could make a cross-tenant user look valid.
        principal_tenant_id = str(user.tenant_id)
        if tenant_id is not None and tenant_id != principal_tenant_id:
            raise PrincipalNotFoundError("User is not registered")

        return UserPrincipal(
            user_id=str(user.id),
            email=user.email,
            role_assignments=self._load_role_assignments(user.id, user.tenant_id),
            direct_permissions=self._load_permission_grants(user.id, user.tenant_id),
            is_service_account=(user.is_service_account or user_status == UserStatus.SERVICE),
            disabled=False,
            tenant_id=principal_tenant_id,
        )

    def _prepare_read_connection(self) -> None:
        """Apply principal read isolation and timeout settings to the transaction."""
        bind = self._session.get_bind()
        connection = self._session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        if bind.dialect.name == "postgresql":
            connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {PRINCIPAL_QUERY_TIMEOUT_MS}"
            )

    def _load_role_assignments(self, user_id: UUID, tenant_id: UUID) -> tuple[RoleAssignment, ...]:
        """Load active role assignments while enforcing a bounded row count."""
        rows = self._session.execute(
            select(UserRoleAssignmentORM, AccessScopeORM)
            .join(AccessScopeORM, UserRoleAssignmentORM.scope_id == AccessScopeORM.id)
            .where(
                UserRoleAssignmentORM.user_id == user_id,
                UserRoleAssignmentORM.tenant_id == tenant_id,
                AccessScopeORM.tenant_id == tenant_id,
                UserRoleAssignmentORM.active.is_(True),
            )
            .order_by(UserRoleAssignmentORM.assigned_at, UserRoleAssignmentORM.id)
            .limit(MAX_ACTIVE_ROLE_ASSIGNMENTS + 1)
        ).all()
        if len(rows) > MAX_ACTIVE_ROLE_ASSIGNMENTS:
            raise PrincipalLimitExceededError(
                f"Principal has more than {MAX_ACTIVE_ROLE_ASSIGNMENTS} active role assignments"
            )
        assignments: list[RoleAssignment] = []
        for assignment, scope in rows:
            assignments.append(
                RoleAssignment(
                    role=_parse_role(assignment.role_key),
                    scope=_to_scope(scope),
                    active=True,
                )
            )
        return tuple(assignments)

    def _load_permission_grants(
        self, user_id: UUID, tenant_id: UUID
    ) -> tuple[PermissionGrant, ...]:
        """Load active direct permission grants while enforcing a bounded row count."""
        rows = self._session.execute(
            select(UserPermissionGrantORM, AccessScopeORM)
            .join(AccessScopeORM, UserPermissionGrantORM.scope_id == AccessScopeORM.id)
            .where(
                UserPermissionGrantORM.user_id == user_id,
                UserPermissionGrantORM.tenant_id == tenant_id,
                AccessScopeORM.tenant_id == tenant_id,
                UserPermissionGrantORM.active.is_(True),
            )
            .order_by(UserPermissionGrantORM.granted_at, UserPermissionGrantORM.id)
            .limit(MAX_ACTIVE_PERMISSION_GRANTS + 1)
        ).all()
        if len(rows) > MAX_ACTIVE_PERMISSION_GRANTS:
            raise PrincipalLimitExceededError(
                f"Principal has more than {MAX_ACTIVE_PERMISSION_GRANTS} active permission grants"
            )
        grants: list[PermissionGrant] = []
        for grant, scope in rows:
            grants.append(
                PermissionGrant(
                    permission=_parse_permission(grant.permission_key),
                    scope=_to_scope(scope),
                    active=True,
                )
            )
        return tuple(grants)


def _parse_uuid(value: str) -> UUID:
    """Parse a trusted user-id header into a UUID."""
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise PrincipalValidationError("user_id must be a valid UUID") from exc


def _parse_tenant_uuid(value: str | None) -> str | None:
    """Normalize optional tenant context into a canonical UUID string."""
    if value is None:
        return None
    try:
        stripped_value = value.strip()
    except AttributeError as exc:
        raise PrincipalValidationError("tenant_id must be a valid UUID") from exc
    if not stripped_value:
        return None
    try:
        return str(UUID(stripped_value))
    except ValueError as exc:
        raise PrincipalValidationError("tenant_id must be a valid UUID") from exc


def _parse_user_status(value: str) -> UserStatus:
    """Resolve a stored user status into the UserStatus enum."""
    try:
        return UserStatus(value)
    except ValueError as exc:
        raise PrincipalDataValidationError(f"Unknown user status in database: {value}") from exc


def _parse_role(value: str) -> RoleKey:
    """Resolve a stored role key into the policy RoleKey enum."""
    try:
        return RoleKey(value)
    except ValueError as exc:
        raise PrincipalDataValidationError(f"Unknown role assignment in database: {value}") from exc


def _parse_permission(value: str) -> Permission:
    """Resolve a stored permission key into the policy Permission enum."""
    try:
        return Permission(value)
    except ValueError as exc:
        raise PrincipalDataValidationError(
            f"Unknown permission grant in database: {value}"
        ) from exc


def _to_scope(scope: AccessScopeORM) -> AccessScope:
    """Convert a stored access scope row into a policy AccessScope."""
    try:
        return AccessScope(ScopeType(scope.scope_type), scope.scope_id)
    except ValueError as exc:
        raise PrincipalDataValidationError(
            f"Unknown access scope in database: {scope.scope_type}"
        ) from exc
