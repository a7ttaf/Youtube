# ============================================================================
# Purpose: Tenant-scoped role-assignment repository: create assignments with
#   actor validation, savepoint-isolated duplicate handling, and a typed
#   conflict contract; plus read and lifecycle operations over them.
# Database/ORM: UserRoleAssignmentORM + AccessScopeORM (SecurityBase) through
#   the caller's SQLAlchemy session; ambient TENANT_CTX supplies tenancy.
# Standards: Typed UserRoleAssignment*Error exceptions; every write inside a
#   begin_nested() savepoint so concurrent duplicates surface as the typed
#   conflict instead of poisoning the outer transaction.
# Blast Radius: Authorization state -- global/company role grants feed policy
#   decisions everywhere; failures here are fail-closed.
# Connections:
#   - File: scripts/bootstrap_operator.py -> idempotent first-run caller whose
#     EXISTING outcome depends on the conflict mapping below.
#   - File: backend/ums_smart_revenue/auth/users.py -> sibling account
#     repository sharing the storage-retry/savepoint pattern.
# ============================================================================
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS, RoleKey
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class UserRoleAssignmentEntry:
    """Data class representing the details of a user role assignment."""

    id: str
    user_id: str
    role_key: str
    scope_type: str
    scope_id: str | None
    assigned_by: str | None
    assigned_at: datetime
    revoked_by: str | None
    revoked_at: datetime | None
    reason: str | None
    active: bool

    def to_api(self) -> dict[str, object]:
        """Convert the user role instance to a dictionary suitable for API responses."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "role_key": self.role_key,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "assigned_by": self.assigned_by,
            "assigned_at": self.assigned_at.isoformat(),
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "reason": self.reason,
            "active": self.active,
        }


class UserRoleAssignmentError(ValueError):
    """Base exception for user role assignment processing errors."""


class UserRoleAssignmentConflictError(UserRoleAssignmentError):
    """Role assignment conflicts with an existing assignment."""


class UserRoleAssignmentNotFoundError(UserRoleAssignmentError):
    """Error raised when a requested user role assignment is not found."""


class UserRoleAssignmentValidationError(UserRoleAssignmentError):
    """Error raised for invalid data during user role assignment."""


class SqlAlchemyUserRoleAssignmentRepository:
    """SQLAlchemy repository for tenant-scoped user role assignments."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind role assignment operations to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    # ============================================================================
    # Purpose: Assign a tenant-scoped role after validating actor, role, scope,
    # duplicate active grants, and service-account restrictions.
    # Database/ORM: UserORM, UserRoleAssignmentORM, AccessScopeORM.
    # Standards: Repository-owned SQLAlchemy writes, typed domain exceptions, and
    # fail-closed tenant and authorization validation.
    # Blast Radius: Authorization and audit-adjacent role grants; no finance or
    # Neo4j projection impact detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/roles.py -> Role and scope
    #     compatibility rules.
    #   - File: backend/ums_smart_revenue/db/security_models.py -> User,
    #     role assignment, and access scope ORM rows.
    # ============================================================================
    def assign_role(
        self,
        *,
        user_id: str,
        role_key: str,
        scope_type: str,
        scope_id: str | None,
        assigned_by: str,
        reason: str,
    ) -> UserRoleAssignmentEntry:
        """Assign a role to a user within a validated tenant scope.

        :raises UserRoleAssignmentConflictError: If an active assignment
            already exists for the user and scope.
        :raises UserRoleAssignmentValidationError: If UUID, role, scope, or
            service-account validation fails.
        :raises UserRoleAssignmentNotFoundError: If the target or actor user
            does not exist in the tenant.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        actor_user_id = _parse_uuid(assigned_by, field_name="assigned_by")
        role = _parse_role(role_key)
        normalized_reason = _normalize_reason(reason)
        target_user = self._require_user(target_user_id)
        self._require_actor_user(actor_user_id)
        self._require_assignable_role(target_user, role)
        _require_compatible_scope_type(role, scope_type)
        scope = self._get_or_create_scope(scope_type=scope_type, scope_id=scope_id)

        existing = self._session.scalars(
            select(UserRoleAssignmentORM).where(
                UserRoleAssignmentORM.tenant_id == self._tenant_id,
                UserRoleAssignmentORM.user_id == target_user_id,
                UserRoleAssignmentORM.role_key == role.value,
                UserRoleAssignmentORM.scope_id == scope.id,
                UserRoleAssignmentORM.active.is_(True),
            )
        ).one_or_none()
        if existing is not None:
            raise UserRoleAssignmentConflictError("Active role assignment already exists")

        row = UserRoleAssignmentORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            user_id=target_user_id,
            role_key=role.value,
            scope_id=scope.id,
            assigned_by=actor_user_id,
            reason=normalized_reason,
            active=True,
        )
        try:
            with self._session.begin_nested():
                # FIX: the add must sit INSIDE the savepoint -- opening
                # begin_nested() flushes pending session state, so an add left
                # outside let a concurrent duplicate INSERT abort the OUTER
                # transaction; this handler then saw PendingRollbackError
                # instead of being able to raise the typed conflict, and the
                # idempotent bootstrap exited with a database failure rather
                # than its EXISTING outcome.
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            duplicate = self._session.scalars(
                select(UserRoleAssignmentORM).where(
                    UserRoleAssignmentORM.tenant_id == self._tenant_id,
                    UserRoleAssignmentORM.user_id == target_user_id,
                    UserRoleAssignmentORM.role_key == role.value,
                    UserRoleAssignmentORM.scope_id == scope.id,
                    UserRoleAssignmentORM.active.is_(True),
                )
            ).one_or_none()
            if duplicate is not None:
                raise UserRoleAssignmentConflictError(
                    "Active role assignment already exists"
                ) from exc
            raise
        return self._to_entry(row, scope)

    def get_assignment(self, *, user_id: str, assignment_id: str) -> UserRoleAssignmentEntry:
        """
        Retrieve a role assignment entry by user and assignment IDs.

        Validate user and assignment IDs, fetch the assignment and its scope,
        and return a UserRoleAssignmentEntry.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        assignment_uuid = _parse_uuid(assignment_id, field_name="assignment_id")
        row = self._session.scalars(
            select(UserRoleAssignmentORM).where(
                UserRoleAssignmentORM.id == assignment_uuid,
                UserRoleAssignmentORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None or row.user_id != target_user_id:
            raise UserRoleAssignmentNotFoundError("Role assignment not found")
        scope = self._session.scalars(
            select(AccessScopeORM).where(
                AccessScopeORM.id == row.scope_id,
                AccessScopeORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if scope is None:
            raise UserRoleAssignmentNotFoundError("Role assignment scope not found")
        return self._to_entry(row, scope)

    def revoke_role(
        self,
        *,
        user_id: str,
        assignment_id: str,
        revoked_by: str,
        reason: str,
    ) -> UserRoleAssignmentEntry:
        """
        Revoke an active role assignment for a user.

        Validate inputs, mark the assignment as revoked with reason and actor,
        and return the updated UserRoleAssignmentEntry.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        assignment_uuid = _parse_uuid(assignment_id, field_name="assignment_id")
        actor_user_id = _parse_uuid(revoked_by, field_name="revoked_by")
        normalized_reason = _normalize_reason(reason)
        self._require_actor_user(actor_user_id)
        row = self._session.scalars(
            select(UserRoleAssignmentORM)
            .where(
                UserRoleAssignmentORM.id == assignment_uuid,
                UserRoleAssignmentORM.tenant_id == self._tenant_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None or row.user_id != target_user_id:
            raise UserRoleAssignmentNotFoundError("Role assignment not found")
        if not row.active:
            raise UserRoleAssignmentConflictError("Role assignment is already revoked")

        now = datetime.now(UTC)
        row.active = False
        row.revoked_by = actor_user_id
        row.revoked_at = now
        row.reason = normalized_reason
        self._session.flush()
        scope = self._session.scalars(
            select(AccessScopeORM).where(
                AccessScopeORM.id == row.scope_id,
                AccessScopeORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if scope is None:
            raise UserRoleAssignmentNotFoundError("Role assignment scope not found")
        return self._to_entry(row, scope)

    def _require_user(self, user_id: UUID) -> UserORM:
        """
        Ensure that the specified user exists and belongs to the current tenant.

        Fetch the UserORM by ID and verify tenant isolation, or raise an error.
        """
        # Keep session.get so the identity-map cache fast-paths repeat lookups
        # in the same transaction; defend cross-tenant access in Python rather
        # than emitting a wider SELECT. Composite FKs introduced in fbf58e1
        # already enforce tenant isolation at the schema level.
        row = self._session.get(UserORM, user_id)
        if row is None or row.tenant_id != self._tenant_id:
            raise UserRoleAssignmentNotFoundError("User not found")
        return row

    def _require_actor_user(self, user_id: UUID) -> None:
        """
        Verify that the actor user exists within the current tenant.

        Query the UserORM ID and raise an error if not found.
        """
        exists = self._session.scalar(
            select(UserORM.id).where(
                UserORM.id == user_id,
                UserORM.tenant_id == self._tenant_id,
            )
        )
        if exists is None:
            raise UserRoleAssignmentNotFoundError("Actor user not found")

    @staticmethod
    def _require_assignable_role(target_user: UserORM, role: RoleKey) -> None:
        """
        Verify that the role can be assigned to the given user.

        Raise an error if a service-only role is assigned to a non-service account.
        """
        definition = ROLE_DEFINITIONS[role]
        if definition.service_only and not target_user.is_service_account:
            raise UserRoleAssignmentValidationError(
                "Service-only roles require a service account user"
            )

    def _get_or_create_scope(self, *, scope_type: str, scope_id: str | None) -> AccessScopeORM:
        """
        Retrieve or create an access scope based on type and optional ID.

        Normalize inputs, attempt to fetch an existing scope, or create a new one
        if none exists, handling concurrent writers gracefully.
        """
        normalized_scope_type, normalized_scope_id = _normalize_scope(scope_type, scope_id)
        scope_filter = (
            AccessScopeORM.tenant_id == self._tenant_id,
            AccessScopeORM.scope_type == normalized_scope_type,
            AccessScopeORM.scope_id.is_(None)
            if normalized_scope_id is None
            else AccessScopeORM.scope_id == normalized_scope_id,
        )
        row = self._session.scalars(select(AccessScopeORM).where(*scope_filter)).one_or_none()
        if row is not None:
            return row

        new_row = AccessScopeORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            label=_scope_label(normalized_scope_type, normalized_scope_id),
        )
        try:
            with self._session.begin_nested():
                self._session.add(new_row)
                self._session.flush()
        except IntegrityError:
            # Another concurrent writer created this scope after our SELECT.
            # The savepoint was rolled back; re-query to return the winning row.
            return self._session.scalars(select(AccessScopeORM).where(*scope_filter)).one()
        return new_row

    @staticmethod
    def _to_entry(row: UserRoleAssignmentORM, scope: AccessScopeORM) -> UserRoleAssignmentEntry:
        """
        Convert ORM row and scope into a UserRoleAssignmentEntry.

        Populate entry fields from the ORM row and associated scope.
        """
        return UserRoleAssignmentEntry(
            id=str(row.id),
            user_id=str(row.user_id),
            role_key=row.role_key,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            assigned_by=str(row.assigned_by) if row.assigned_by else None,
            assigned_at=row.assigned_at,
            revoked_by=str(row.revoked_by) if row.revoked_by else None,
            revoked_at=row.revoked_at,
            reason=row.reason,
            active=row.active,
        )


def _parse_uuid(value: str, *, field_name: str) -> UUID:
    """
    Parse a string into a UUID for the given field name.

    Raises a validation error if the value is not a valid UUID.
    """
    try:
        return UUID(value)
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(f"{field_name} must be a valid UUID") from exc


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or bootstrap."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise UserRoleAssignmentValidationError("tenant_id must be a valid UUID") from exc


def _parse_role(value: str) -> RoleKey:
    """
    Parse and validate a role key string into a RoleKey enum.

    Raises a validation error for unknown role keys.
    """
    try:
        return RoleKey(_normalize_required_string(value, "role_key"))
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(f"Unknown role_key: {value}") from exc


def _require_compatible_scope_type(role: RoleKey, scope_type: str) -> None:
    """
    Ensure the scope type is allowed for the given role.

    Raise a validation error if the scope type is not compatible with the role.
    """
    definition = ROLE_DEFINITIONS[role]
    normalized = scope_type.strip().lower()
    if normalized not in definition.allowed_scope_types:
        allowed = ", ".join(sorted(definition.allowed_scope_types))
        raise UserRoleAssignmentValidationError(
            f"Role {role.value!r} cannot be assigned to scope type "
            f"{normalized!r}; allowed: {allowed}"
        )


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str | None]:
    """
    Normalize and validate scope type and optional scope ID.

    Validate that scope ID requirements are met for the specified scope type.
    """
    try:
        parsed_scope_type = ScopeType(_normalize_required_string(scope_type, "scope_type"))
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(f"Unknown scope_type: {scope_type}") from exc
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else scope_id
    if parsed_scope_type == ScopeType.GLOBAL:
        if normalized_scope_id is not None:
            raise UserRoleAssignmentValidationError("scope_id must be omitted for global scope")
        return parsed_scope_type.value, None
    if not normalized_scope_id:
        raise UserRoleAssignmentValidationError(
            f"scope_id is required for scope type: {parsed_scope_type.value}"
        )
    return parsed_scope_type.value, normalized_scope_id


def _normalize_required_string(value: str, field_name: str) -> str:
    """
    Ensure a string field is not blank after stripping whitespace.

    Return the stripped string or raise a validation error if blank.
    """
    normalized = value.strip()
    if not normalized:
        raise UserRoleAssignmentValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_reason(value: str) -> str:
    """
    Normalize the reason string by stripping whitespace.

    Ensures the reason is not blank.
    """
    return _normalize_required_string(value, "reason")


def _scope_label(scope_type: str, scope_id: str | None) -> str:
    """
    Generate a human-readable label for a scope.

    Return 'Global' for global scopes or 'type:id' for scoped entries.
    """
    return "Global" if scope_id is None else f"{scope_type}:{scope_id}"
