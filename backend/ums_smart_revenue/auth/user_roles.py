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

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class UserRoleAssignmentEntry:
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
    pass


class UserRoleAssignmentConflictError(UserRoleAssignmentError):
    pass


class UserRoleAssignmentNotFoundError(UserRoleAssignmentError):
    pass


class UserRoleAssignmentValidationError(UserRoleAssignmentError):
    pass


class SqlAlchemyUserRoleAssignmentRepository:
    def __init__(self, session: Session):
        self._session = session
        self._tenant_id = _DEFAULT_TENANT_UUID

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
            raise UserRoleAssignmentConflictError(
                "Active role assignment already exists"
            )

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
        self._session.add(row)
        try:
            with self._session.begin_nested():
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

    def get_assignment(
        self, *, user_id: str, assignment_id: str
    ) -> UserRoleAssignmentEntry:
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
        # Keep session.get so the identity-map cache fast-paths repeat lookups
        # in the same transaction; defend cross-tenant access in Python rather
        # than emitting a wider SELECT. Composite FKs introduced in fbf58e1
        # already enforce tenant isolation at the schema level.
        row = self._session.get(UserORM, user_id)
        if row is None or row.tenant_id != self._tenant_id:
            raise UserRoleAssignmentNotFoundError("User not found")
        return row

    def _require_actor_user(self, user_id: UUID) -> None:
        if self._session.get(UserORM, user_id) is None:
            raise UserRoleAssignmentNotFoundError("Actor user not found")

    def _require_assignable_role(self, target_user: UserORM, role: RoleKey) -> None:
        definition = ROLE_DEFINITIONS[role]
        if definition.service_only and not target_user.is_service_account:
            raise UserRoleAssignmentValidationError(
                "Service-only roles require a service account user"
            )

    def _get_or_create_scope(
        self, *, scope_type: str, scope_id: str | None
    ) -> AccessScopeORM:
        normalized_scope_type, normalized_scope_id = _normalize_scope(
            scope_type, scope_id
        )
        scope_filter = (
            AccessScopeORM.tenant_id == self._tenant_id,
            AccessScopeORM.scope_type == normalized_scope_type,
            AccessScopeORM.scope_id.is_(None)
            if normalized_scope_id is None
            else AccessScopeORM.scope_id == normalized_scope_id,
        )
        row = self._session.scalars(
            select(AccessScopeORM).where(*scope_filter)
        ).one_or_none()
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
            return self._session.scalars(
                select(AccessScopeORM).where(*scope_filter)
            ).one()
        return new_row

    @staticmethod
    def _to_entry(
        row: UserRoleAssignmentORM, scope: AccessScopeORM
    ) -> UserRoleAssignmentEntry:
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
    try:
        return UUID(value)
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(
            f"{field_name} must be a valid UUID"
        ) from exc


def _parse_role(value: str) -> RoleKey:
    try:
        return RoleKey(_normalize_required_string(value, "role_key"))
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(f"Unknown role_key: {value}") from exc


def _require_compatible_scope_type(role: RoleKey, scope_type: str) -> None:
    definition = ROLE_DEFINITIONS[role]
    normalized = scope_type.strip().lower()
    if normalized not in definition.allowed_scope_types:
        allowed = ", ".join(sorted(definition.allowed_scope_types))
        raise UserRoleAssignmentValidationError(
            f"Role {role.value!r} cannot be assigned to scope type "
            f"{normalized!r}; allowed: {allowed}"
        )


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str | None]:
    try:
        parsed_scope_type = ScopeType(
            _normalize_required_string(scope_type, "scope_type")
        )
    except ValueError as exc:
        raise UserRoleAssignmentValidationError(
            f"Unknown scope_type: {scope_type}"
        ) from exc
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else scope_id
    if parsed_scope_type == ScopeType.GLOBAL:
        if normalized_scope_id is not None:
            raise UserRoleAssignmentValidationError(
                "scope_id must be omitted for global scope"
            )
        return parsed_scope_type.value, None
    if not normalized_scope_id:
        raise UserRoleAssignmentValidationError(
            f"scope_id is required for scope type: {parsed_scope_type.value}"
        )
    return parsed_scope_type.value, normalized_scope_id


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserRoleAssignmentValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_reason(value: str) -> str:
    return _normalize_required_string(value, "reason")


def _scope_label(scope_type: str, scope_id: str | None) -> str:
    return "Global" if scope_id is None else f"{scope_type}:{scope_id}"
