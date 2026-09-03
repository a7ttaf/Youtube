from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserPermissionGrantORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)

_ORG_SCOPE_TYPES = frozenset(
    {
        ScopeType.GLOBAL.value,
        ScopeType.SECTOR.value,
        ScopeType.COMPANY.value,
        ScopeType.CHANNEL.value,
    }
)
_FINANCE_MONTH_SCOPE_TYPES = frozenset({ScopeType.GLOBAL.value, ScopeType.FINANCE_MONTH.value})
_FINANCE_DATA_SCOPE_TYPES = _FINANCE_MONTH_SCOPE_TYPES
_EXPORT_SCOPE_TYPES = _ORG_SCOPE_TYPES | frozenset({ScopeType.EXPORT.value})
_CONNECTOR_SCOPE_TYPES = frozenset({ScopeType.GLOBAL.value, ScopeType.CONNECTOR.value})
_GLOBAL_SCOPE_TYPES = frozenset({ScopeType.GLOBAL.value})

PERMISSION_SCOPE_TYPES: dict[Permission, frozenset[str]] = {
    Permission.VIEW_ANALYTICS: _ORG_SCOPE_TYPES,
    Permission.VIEW_CONFIDENCE: _ORG_SCOPE_TYPES,
    Permission.VIEW_REVENUE: _ORG_SCOPE_TYPES,
    Permission.VIEW_FINALIZED_PAYMENTS: _FINANCE_DATA_SCOPE_TYPES,
    Permission.VIEW_BANK_RECONCILIATION: _FINANCE_DATA_SCOPE_TYPES,
    Permission.MANAGE_BANK_RECONCILIATION: _FINANCE_DATA_SCOPE_TYPES,
    Permission.CREATE_MANUAL_OVERRIDE: _ORG_SCOPE_TYPES,
    Permission.APPROVE_MANUAL_OVERRIDE: _ORG_SCOPE_TYPES,
    Permission.LOCK_FINANCE_MONTH: _FINANCE_MONTH_SCOPE_TYPES,
    Permission.UNLOCK_FINANCE_MONTH: _FINANCE_MONTH_SCOPE_TYPES,
    Permission.CHANGE_ALLOCATION_RULE: _FINANCE_MONTH_SCOPE_TYPES,
    Permission.EXPORT_ANALYTICS_REPORT: _EXPORT_SCOPE_TYPES,
    Permission.EXPORT_REVENUE_REPORT: _EXPORT_SCOPE_TYPES,
    Permission.MANAGE_EXPORT_TEMPLATES: frozenset({ScopeType.GLOBAL.value, ScopeType.EXPORT.value}),
    Permission.IMPORT_MANUAL_REVENUE: _GLOBAL_SCOPE_TYPES,
    Permission.MANAGE_CHANNELS: _ORG_SCOPE_TYPES,
    Permission.MANAGE_ORG_MAPPING: _ORG_SCOPE_TYPES,
    Permission.MANAGE_GROUPS: _ORG_SCOPE_TYPES,
    Permission.VIEW_CONNECTOR_HEALTH: _CONNECTOR_SCOPE_TYPES,
    Permission.RUN_CONNECTOR_JOBS: _CONNECTOR_SCOPE_TYPES,
    Permission.MANAGE_CONNECTORS: _CONNECTOR_SCOPE_TYPES,
    Permission.VIEW_RAW_FILES: _CONNECTOR_SCOPE_TYPES,
    Permission.VIEW_AUDIT_LOG: _GLOBAL_SCOPE_TYPES,
    Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS: _GLOBAL_SCOPE_TYPES,
    Permission.MANAGE_USERS: _GLOBAL_SCOPE_TYPES,
    Permission.ASSIGN_ROLES: _GLOBAL_SCOPE_TYPES,
    Permission.MANAGE_PLATFORM_SETTINGS: _GLOBAL_SCOPE_TYPES,
}


if set(PERMISSION_SCOPE_TYPES) != set(Permission):
    missing = sorted(
        permission.value for permission in set(Permission) - set(PERMISSION_SCOPE_TYPES)
    )
    extra = sorted(permission.value for permission in set(PERMISSION_SCOPE_TYPES) - set(Permission))
    raise RuntimeError(f"Permission scope coverage mismatch. missing={missing} extra={extra}")


@dataclass(frozen=True)
class UserPermissionGrantEntry:
    """One tenant-scoped direct permission grant record."""

    id: str
    user_id: str
    permission_key: str
    scope_type: str
    scope_id: str | None
    granted_by: str | None
    granted_at: datetime
    revoked_by: str | None
    revoked_at: datetime | None
    grant_reason: str | None
    revoke_reason: str | None
    active: bool

    def to_api(self) -> dict[str, object]:
        """Serialize the grant entry to a JSON-safe dict for API responses."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "permission_key": self.permission_key,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat(),
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "grant_reason": self.grant_reason,
            "revoke_reason": self.revoke_reason,
            "active": self.active,
        }


class UserPermissionGrantError(ValueError):
    """Base typed error for direct permission grant mutations."""


class UserPermissionGrantConflictError(UserPermissionGrantError):
    """The grant already exists (or conflicts with a pending savepoint write)."""


class UserPermissionGrantNotFoundError(UserPermissionGrantError):
    """No grant matches the requested tenant/user/permission/scope."""


class UserPermissionGrantValidationError(UserPermissionGrantError):
    """A grant field failed normalization or scope validation."""


class SqlAlchemyUserPermissionGrantRepository:
    """PostgreSQL repository for tenant-scoped direct permission grants."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind direct permission grants to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def grant_permission(
        self,
        *,
        user_id: str,
        permission_key: str,
        scope_type: str,
        scope_id: str | None,
        granted_by: str,
        reason: str,
    ) -> UserPermissionGrantEntry:
        """Grant a direct permission to a user within a scope.

        Raises UserPermissionGrantConflictError if an active grant already exists,
        UserPermissionGrantNotFoundError if the target or actor user does not exist,
        and UserPermissionGrantValidationError for invalid inputs.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        actor_user_id = _parse_uuid(granted_by, field_name="granted_by")
        permission = _parse_permission(permission_key)
        normalized_reason = _normalize_reason(reason)
        self._require_user(target_user_id, field_name="user_id")
        self._require_user(actor_user_id, field_name="granted_by")
        _require_compatible_scope_type(permission, scope_type)
        scope = self._get_or_create_scope(scope_type=scope_type, scope_id=scope_id)

        existing = self._session.scalars(
            select(UserPermissionGrantORM).where(
                UserPermissionGrantORM.tenant_id == self._tenant_id,
                UserPermissionGrantORM.user_id == target_user_id,
                UserPermissionGrantORM.permission_key == permission.value,
                UserPermissionGrantORM.scope_id == scope.id,
                UserPermissionGrantORM.active.is_(True),
            )
        ).one_or_none()
        if existing is not None:
            raise UserPermissionGrantConflictError("Active permission grant already exists")

        row = UserPermissionGrantORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            user_id=target_user_id,
            permission_key=permission.value,
            scope_id=scope.id,
            granted_by=actor_user_id,
            reason=normalized_reason,
            active=True,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            duplicate = self._session.scalars(
                select(UserPermissionGrantORM).where(
                    UserPermissionGrantORM.tenant_id == self._tenant_id,
                    UserPermissionGrantORM.user_id == target_user_id,
                    UserPermissionGrantORM.permission_key == permission.value,
                    UserPermissionGrantORM.scope_id == scope.id,
                    UserPermissionGrantORM.active.is_(True),
                )
            ).one_or_none()
            if duplicate is not None:
                raise UserPermissionGrantConflictError(
                    "Active permission grant already exists"
                ) from exc
            if not self._user_exists_in_db(target_user_id):
                raise UserPermissionGrantNotFoundError("user_id not found") from exc
            if not self._user_exists_in_db(actor_user_id):
                raise UserPermissionGrantNotFoundError("granted_by not found") from exc
            raise
        return self._to_entry(row, scope)

    def get_grant(self, *, user_id: str, grant_id: str) -> UserPermissionGrantEntry:
        """Return the permission grant identified by grant_id scoped to user_id.

        Raises UserPermissionGrantNotFoundError if the grant does not exist or
        belongs to a different user.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        grant_uuid = _parse_uuid(grant_id, field_name="grant_id")
        row = self._session.scalars(
            select(UserPermissionGrantORM).where(
                UserPermissionGrantORM.id == grant_uuid,
                UserPermissionGrantORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None or row.user_id != target_user_id:
            raise UserPermissionGrantNotFoundError("Permission grant not found")
        scope = self._session.scalars(
            select(AccessScopeORM).where(
                AccessScopeORM.id == row.scope_id,
                AccessScopeORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if scope is None:
            raise UserPermissionGrantNotFoundError("Permission grant scope not found")
        return self._to_entry(row, scope)

    def revoke_permission(
        self,
        *,
        user_id: str,
        grant_id: str,
        revoked_by: str,
        reason: str,
    ) -> UserPermissionGrantEntry:
        """Revoke an active permission grant.

        Acquires a row-level lock before mutating to prevent double-revoke races.
        Raises UserPermissionGrantConflictError if already revoked,
        UserPermissionGrantNotFoundError if the grant or actor does not exist.
        """
        target_user_id = _parse_uuid(user_id, field_name="user_id")
        grant_uuid = _parse_uuid(grant_id, field_name="grant_id")
        actor_user_id = _parse_uuid(revoked_by, field_name="revoked_by")
        normalized_reason = _normalize_reason(reason)
        self._require_user(actor_user_id, field_name="revoked_by")
        row = self._session.scalars(
            select(UserPermissionGrantORM)
            .where(
                UserPermissionGrantORM.id == grant_uuid,
                UserPermissionGrantORM.tenant_id == self._tenant_id,
                UserPermissionGrantORM.user_id == target_user_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise UserPermissionGrantNotFoundError("Permission grant not found")
        if not row.active:
            raise UserPermissionGrantConflictError("Permission grant is already revoked")

        now = datetime.now(UTC)
        row.active = False
        row.revoked_by = actor_user_id
        row.revoked_at = now
        row.revoke_reason = normalized_reason
        try:
            self._session.flush()
        except IntegrityError as exc:
            if not self._user_exists_in_db(actor_user_id):
                raise UserPermissionGrantNotFoundError("revoked_by not found") from exc
            raise
        scope = self._session.scalars(
            select(AccessScopeORM).where(
                AccessScopeORM.id == row.scope_id,
                AccessScopeORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if scope is None:
            raise UserPermissionGrantNotFoundError("Permission grant scope not found")
        return self._to_entry(row, scope)

    def _require_user(self, user_id: UUID, *, field_name: str = "user_id") -> UserORM:
        """Look up a user by UUID; raise UserPermissionGrantNotFoundError if absent."""
        # Keep session.get so the identity-map cache fast-paths repeat lookups
        # in the same transaction; defend cross-tenant access in Python rather
        # than emitting a wider SELECT. Composite FKs introduced in fbf58e1
        # already enforce tenant isolation at the schema level.
        row = self._session.get(UserORM, user_id)
        if row is None or row.tenant_id != self._tenant_id:
            raise UserPermissionGrantNotFoundError(f"{field_name} not found")
        return row

    def _user_exists_in_db(self, user_id: UUID) -> bool:
        """Return whether a user row exists, bypassing identity-map-only hits."""
        return (
            self._session.scalar(
                select(UserORM.id).where(
                    UserORM.id == user_id,
                    UserORM.tenant_id == self._tenant_id,
                )
            )
            is not None
        )

    def _get_or_create_scope(self, *, scope_type: str, scope_id: str | None) -> AccessScopeORM:
        """Return an existing AccessScope row or create one.

        Handles concurrent inserts via savepoint.
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
            return self._session.scalars(select(AccessScopeORM).where(*scope_filter)).one()
        return new_row

    @staticmethod
    def _to_entry(row: UserPermissionGrantORM, scope: AccessScopeORM) -> UserPermissionGrantEntry:
        """Map ORM row + scope row to an immutable domain entry."""
        return UserPermissionGrantEntry(
            id=str(row.id),
            user_id=str(row.user_id),
            permission_key=row.permission_key,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            granted_by=str(row.granted_by) if row.granted_by else None,
            granted_at=row.granted_at,
            revoked_by=str(row.revoked_by) if row.revoked_by else None,
            revoked_at=row.revoked_at,
            grant_reason=row.reason,
            revoke_reason=row.revoke_reason,
            active=row.active,
        )


def _parse_uuid(value: str, *, field_name: str) -> UUID:
    """Parse a UUID string; raise validation error on invalid format."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise UserPermissionGrantValidationError(f"{field_name} must be a valid UUID") from exc


def _parse_permission(value: str) -> Permission:
    """Resolve a permission key string to a Permission enum member."""
    try:
        return Permission(_normalize_required_string(value, "permission_key"))
    except ValueError as exc:
        raise UserPermissionGrantValidationError(f"Unknown permission_key: {value}") from exc


def _require_compatible_scope_type(permission: Permission, scope_type: str) -> None:
    """Raise validation error when scope_type is not allowed for permission."""
    normalized = scope_type.strip().lower()
    allowed_scope_types = PERMISSION_SCOPE_TYPES[permission]
    if normalized not in allowed_scope_types:
        allowed = ", ".join(sorted(allowed_scope_types))
        raise UserPermissionGrantValidationError(
            f"Permission {permission.value!r} cannot be granted to scope type "
            f"{normalized!r}; allowed: {allowed}"
        )


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
        raise UserPermissionGrantValidationError("tenant_id must be a valid UUID") from exc


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str | None]:
    """Validate and normalize scope_type/scope_id; enforce global-scope rules."""
    try:
        parsed_scope_type = ScopeType(_normalize_required_string(scope_type, "scope_type"))
    except ValueError as exc:
        raise UserPermissionGrantValidationError(f"Unknown scope_type: {scope_type}") from exc
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else scope_id
    if parsed_scope_type == ScopeType.GLOBAL:
        if normalized_scope_id is not None:
            raise UserPermissionGrantValidationError("scope_id must be omitted for global scope")
        return parsed_scope_type.value, None
    if not normalized_scope_id:
        raise UserPermissionGrantValidationError(
            f"scope_id is required for scope type: {parsed_scope_type.value}"
        )
    return parsed_scope_type.value, normalized_scope_id


def _normalize_required_string(value: str, field_name: str) -> str:
    """Strip one required string field and refuse empty/whitespace input."""
    normalized = value.strip()
    if not normalized:
        raise UserPermissionGrantValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_reason(value: str) -> str:
    """Normalize an optional free-text reason to a bounded, stripped value."""
    return _normalize_required_string(value, "reason")


def _scope_label(scope_type: str, scope_id: str | None) -> str:
    """Render one grant scope as a compact human-readable label."""
    return "Global" if scope_id is None else f"{scope_type}:{scope_id}"
