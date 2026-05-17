import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.user_permissions import UserPermissionGrantEntry
from ums_smart_revenue.auth.user_roles import UserRoleAssignmentEntry
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

logger = logging.getLogger(__name__)
T = TypeVar("T")

USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUS_SERVICE = "service"
USER_STATUSES = frozenset(
    {USER_STATUS_ACTIVE, USER_STATUS_DISABLED, USER_STATUS_SERVICE}
)
USER_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email_lower"
USER_EMAIL_MAX_LENGTH = 320
USER_DISPLAY_NAME_MAX_LENGTH = 200
USER_LIST_MAX_OFFSET = 10_000
_EMAIL_CONFLICT_SAMPLE_LIMIT = 2
USER_ACCOUNT_STORAGE_ATTEMPTS = 2
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class UserAccountEntry:
    """Immutable user account snapshot returned by repository operations."""

    id: str
    email: str
    display_name: str
    status: str
    is_service_account: bool
    created_at: datetime
    updated_at: datetime

    def to_api(self) -> dict[str, object]:
        """Serialize the account snapshot for API responses and audit details."""
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "status": self.status,
            "is_service_account": self.is_service_account,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class UserAccessProfileEntry:
    """User account plus active scoped authorization rows for admin review."""

    user: UserAccountEntry
    role_assignments: tuple[UserRoleAssignmentEntry, ...]
    direct_permissions: tuple[UserPermissionGrantEntry, ...]

    def to_api(self) -> dict[str, object]:
        """Serialize the access profile for API responses."""
        return {
            "user": self.user.to_api(),
            "role_assignments": [
                assignment.to_api() for assignment in self.role_assignments
            ],
            "direct_permissions": [
                grant.to_api() for grant in self.direct_permissions
            ],
        }


class UserAccountError(ValueError):
    """Base class for user account domain failures exposed to API handlers."""

    pass


class UserAccountConflictError(UserAccountError):
    """Raised when a requested account mutation conflicts with stored data."""

    pass


class UserAccountNotFoundError(UserAccountError):
    """Raised when a requested user account does not exist."""

    pass


class UserAccountValidationError(UserAccountError):
    """Raised when account input cannot be normalized into a safe value."""

    pass


class UserAccountStorageError(UserAccountError):
    """Raised when account storage is unavailable after retryable attempts."""

    pass


class UserAccountServiceAccountPolicyError(UserAccountError):
    """Raised when a stale service-account row blocks a non-owner update."""

    pass


class SqlAlchemyUserAccountRepository:
    """SQLAlchemy-backed repository for guarded user account lifecycle changes."""

    def __init__(self, session: Session):
        """Bind repository operations to the request-scoped SQLAlchemy session."""
        self._session = session
        self._tenant_id = _DEFAULT_TENANT_UUID

    def create_user(
        self,
        *,
        email: str,
        display_name: str,
        is_service_account: bool,
    ) -> UserAccountEntry:
        """Create a user account and normalize email, display name, and status."""
        normalized_email = _normalize_email(email)
        normalized_display_name = _normalize_bounded_string(
            display_name,
            "display_name",
            max_length=USER_DISPLAY_NAME_MAX_LENGTH,
        )
        normalized_is_service_account = _normalize_service_account_flag(
            is_service_account
        )

        def operation() -> UserAccountEntry:
            """Attempt one account-create write against the current session."""
            if self._email_exists(normalized_email):
                raise UserAccountConflictError("User email already exists")

            row = UserORM(
                id=uuid4(),
                tenant_id=self._tenant_id,
                email=normalized_email,
                display_name=normalized_display_name,
                status=(
                    USER_STATUS_SERVICE
                    if normalized_is_service_account
                    else USER_STATUS_ACTIVE
                ),
                is_service_account=normalized_is_service_account,
            )
            try:
                self._session.add(row)
                self._session.flush()
            except IntegrityError as exc:
                self._session.rollback()
                if _is_email_constraint_violation(exc) or self._email_exists(
                    normalized_email
                ):
                    raise UserAccountConflictError("User email already exists") from exc
                raise UserAccountConflictError(
                    "User account violates database constraints"
                ) from exc
            return self._to_entry(row)

        return self._run_with_storage_retries(operation)

    def get_user(self, *, user_id: str) -> UserAccountEntry:
        """Load one account by UUID string, returning a domain not-found error."""
        user_uuid = _parse_uuid(user_id, field_name="user_id")

        def operation() -> UserAccountEntry:
            """Attempt one account lookup against the current session."""
            row = self._session.scalars(
                select(UserORM).where(
                    UserORM.id == user_uuid,
                    UserORM.tenant_id == self._tenant_id,
                )
            ).one_or_none()
            if row is None:
                raise UserAccountNotFoundError("User not found")
            return self._to_entry(row)

        return self._run_with_storage_retries(operation)

    def list_users(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        cursor_email: str | None = None,
        cursor_id: str | None = None,
    ) -> tuple[tuple[UserAccountEntry, ...], bool, dict[str, str] | None]:
        """Return a stable page of user accounts sorted by normalized email."""
        normalized_limit = _normalize_limit(limit)
        normalized_offset = _normalize_offset(offset)
        normalized_status = _normalize_status(status) if status is not None else None
        cursor = _normalize_user_cursor(
            cursor_email=cursor_email,
            cursor_id=cursor_id,
            offset=normalized_offset,
        )

        def operation() -> tuple[
            tuple[UserAccountEntry, ...], bool, dict[str, str] | None
        ]:
            """Attempt one user-list read against the current session."""
            email_sort_key = func.lower(UserORM.email)
            statement = (
                select(UserORM)
                .where(UserORM.tenant_id == self._tenant_id)
                .order_by(email_sort_key, UserORM.id)
            )
            if normalized_status is not None:
                statement = statement.where(UserORM.status == normalized_status)
            if cursor is not None:
                cursor_email_value, cursor_uuid = cursor
                statement = statement.where(
                    or_(
                        email_sort_key > cursor_email_value,
                        and_(
                            email_sort_key == cursor_email_value,
                            UserORM.id > cursor_uuid,
                        ),
                    )
                )
            elif normalized_offset:
                statement = statement.offset(normalized_offset)
            rows = self._session.scalars(
                statement.limit(normalized_limit + 1)
            ).all()
            items = tuple(self._to_entry(row) for row in rows[:normalized_limit])
            has_more = len(rows) > normalized_limit
            return (
                items,
                has_more,
                _next_user_cursor(items) if has_more else None,
            )

        return self._run_with_storage_retries(operation)

    def get_access_profile(self, *, user_id: str) -> UserAccessProfileEntry:
        """Load one account with active role assignments and direct grants."""
        user_uuid = _parse_uuid(user_id, field_name="user_id")

        def operation() -> UserAccessProfileEntry:
            """Attempt one access-profile read against the current session."""
            account_row = self._session.scalars(
                select(UserORM).where(
                    UserORM.id == user_uuid,
                    UserORM.tenant_id == self._tenant_id,
                )
            ).one_or_none()
            if account_row is None:
                raise UserAccountNotFoundError("User not found")

            role_rows = self._session.execute(
                select(UserRoleAssignmentORM, AccessScopeORM)
                .join(
                    AccessScopeORM,
                    (UserRoleAssignmentORM.scope_id == AccessScopeORM.id)
                    & (AccessScopeORM.tenant_id == self._tenant_id),
                )
                .where(
                    UserRoleAssignmentORM.user_id == user_uuid,
                    UserRoleAssignmentORM.tenant_id == self._tenant_id,
                    UserRoleAssignmentORM.active.is_(True),
                )
                .order_by(UserRoleAssignmentORM.assigned_at, UserRoleAssignmentORM.id)
            ).all()
            permission_rows = self._session.execute(
                select(UserPermissionGrantORM, AccessScopeORM)
                .join(
                    AccessScopeORM,
                    (UserPermissionGrantORM.scope_id == AccessScopeORM.id)
                    & (AccessScopeORM.tenant_id == self._tenant_id),
                )
                .where(
                    UserPermissionGrantORM.user_id == user_uuid,
                    UserPermissionGrantORM.tenant_id == self._tenant_id,
                    UserPermissionGrantORM.active.is_(True),
                )
                .order_by(UserPermissionGrantORM.granted_at, UserPermissionGrantORM.id)
            ).all()
            return UserAccessProfileEntry(
                user=self._to_entry(account_row),
                role_assignments=tuple(
                    _role_access_to_entry(row, scope) for row, scope in role_rows
                ),
                direct_permissions=tuple(
                    _permission_access_to_entry(row, scope)
                    for row, scope in permission_rows
                ),
            )

        return self._run_with_storage_retries(operation)

    def update_user(
        self,
        *,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
        status: str | None = None,
        service_account_updates_allowed: bool = True,
    ) -> UserAccountEntry:
        """Update account metadata or lifecycle status after compatibility checks."""
        user_uuid = _parse_uuid(user_id, field_name="user_id")

        normalized_email = None
        normalized_display_name = None
        normalized_status = None

        if email is not None:
            normalized_email = _normalize_email(email)

        if display_name is not None:
            normalized_display_name = _normalize_bounded_string(
                display_name,
                "display_name",
                max_length=USER_DISPLAY_NAME_MAX_LENGTH,
            )

        if status is not None:
            normalized_status = _normalize_status(status)

        def operation() -> UserAccountEntry:
            """Attempt one account-update write against the current session."""
            row = self._session.scalars(
                select(UserORM).where(
                    UserORM.id == user_uuid,
                    UserORM.tenant_id == self._tenant_id,
                )
            ).one_or_none()
            if row is None:
                raise UserAccountNotFoundError("User not found")

            if row.is_service_account and not service_account_updates_allowed:
                raise UserAccountServiceAccountPolicyError(
                    "Service account management requires Super Owner"
                )

            if normalized_email is not None and (
                self._email_exists(normalized_email, excluding_user_id=user_uuid)
            ):
                raise UserAccountConflictError("User email already exists")

            if normalized_status is not None:
                _require_compatible_status(row, normalized_status)

            try:
                if normalized_email is not None:
                    row.email = normalized_email
                if normalized_display_name is not None:
                    row.display_name = normalized_display_name
                if normalized_status is not None:
                    row.status = normalized_status
                self._session.flush()
            except IntegrityError as exc:
                self._session.rollback()
                if normalized_email is not None and (
                    _is_email_constraint_violation(exc)
                    or self._email_exists(normalized_email, excluding_user_id=user_uuid)
                ):
                    raise UserAccountConflictError("User email already exists") from exc
                raise UserAccountConflictError(
                    "User account violates database constraints"
                ) from exc
            return self._to_entry(row)

        return self._run_with_storage_retries(operation)

    def _run_with_storage_retries(
        self, operation: Callable[[], T]
    ) -> T:
        """Retry transient storage failures once and fail closed otherwise."""
        for attempt_index in range(USER_ACCOUNT_STORAGE_ATTEMPTS):
            try:
                return operation()
            except SQLAlchemyError as exc:
                self._session.rollback()
                if (
                    attempt_index + 1 >= USER_ACCOUNT_STORAGE_ATTEMPTS
                    or not _is_retryable_user_storage_error(exc)
                ):
                    raise UserAccountStorageError(
                        "User account storage unavailable"
                    ) from exc
                logger.warning(
                    "Retrying user account storage operation after transient failure"
                )
        raise RuntimeError("unreachable user account retry state")

    def _email_exists(
        self, email: str, *, excluding_user_id: UUID | None = None
    ) -> bool:
        """Return whether a normalized email already belongs to another user."""
        criteria = [
            UserORM.tenant_id == self._tenant_id,
            func.lower(UserORM.email) == email,
        ]
        if excluding_user_id is not None:
            criteria.append(UserORM.id != excluding_user_id)
        conflicts = self._session.scalars(
            select(UserORM.id).where(*criteria).limit(_EMAIL_CONFLICT_SAMPLE_LIMIT)
        ).all()
        if len(conflicts) > 1:
            logger.warning("Multiple existing users matched a normalized email lookup")
        return bool(conflicts)

    @staticmethod
    def _to_entry(row: UserORM) -> UserAccountEntry:
        """Map a SQLAlchemy user row into the repository response object."""
        return UserAccountEntry(
            id=str(row.id),
            email=row.email,
            display_name=row.display_name,
            status=row.status,
            is_service_account=row.is_service_account,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _parse_uuid(value: object, *, field_name: str) -> UUID:
    """Parse a UUID input and expose invalid values as domain validation errors."""
    try:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string")
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise UserAccountValidationError(f"{field_name} must be a valid UUID") from exc


def _normalize_email(value: str) -> str:
    """Normalize an account email and reject malformed local or domain parts."""
    normalized = _normalize_bounded_string(
        value, "email", max_length=USER_EMAIL_MAX_LENGTH
    ).lower()
    if normalized.count("@") != 1:
        raise UserAccountValidationError("email must be a valid email address")
    local, domain = normalized.split("@", maxsplit=1)
    if (
        not local
        or not domain
        or any(character.isspace() for character in normalized)
        or domain.startswith(".")
        or domain.endswith(".")
        or "." not in domain
        or any(part == "" for part in domain.split("."))
    ):
        raise UserAccountValidationError("email must be a valid email address")
    return normalized


def _normalize_status(value: str) -> str:
    """Normalize a lifecycle status and enforce the supported status set."""
    normalized = _normalize_required_string(value, "status").lower()
    if normalized not in USER_STATUSES:
        allowed = ", ".join(sorted(USER_STATUSES))
        raise UserAccountValidationError(
            f"Unknown status: {normalized}; allowed: {allowed}"
        )
    return normalized


def _normalize_limit(value: int) -> int:
    """Normalize and bound list page sizes for repository callers."""
    if not isinstance(value, int):
        raise UserAccountValidationError("limit must be an integer")
    if value < 1 or value > 100:
        raise UserAccountValidationError("limit must be between 1 and 100")
    return value


def _normalize_offset(value: int) -> int:
    """Normalize list offsets for repository callers."""
    if not isinstance(value, int):
        raise UserAccountValidationError("offset must be an integer")
    if value < 0:
        raise UserAccountValidationError("offset must be greater than or equal to 0")
    if value > USER_LIST_MAX_OFFSET:
        raise UserAccountValidationError(
            f"offset must be less than or equal to {USER_LIST_MAX_OFFSET}"
        )
    return value


def _normalize_user_cursor(
    *,
    cursor_email: str | None,
    cursor_id: str | None,
    offset: int,
) -> tuple[str, UUID] | None:
    """Normalize the user-list keyset cursor and reject ambiguous paging."""
    if (cursor_email is None) != (cursor_id is None):
        raise UserAccountValidationError(
            "cursor_email and cursor_id must be provided together"
        )
    if cursor_email is None or cursor_id is None:
        return None
    if offset != 0:
        raise UserAccountValidationError("offset must be 0 when cursor is provided")
    return (
        _normalize_email(cursor_email),
        _parse_uuid(cursor_id, field_name="cursor_id"),
    )


def _normalize_service_account_flag(value: object) -> bool:
    """Reject non-boolean service-account flags before persistence."""
    if not isinstance(value, bool):
        raise UserAccountValidationError("is_service_account must be a boolean")
    return value


def _require_compatible_status(row: UserORM, status: str) -> None:
    """Enforce human versus service-account lifecycle status invariants."""
    if status == USER_STATUS_SERVICE and not row.is_service_account:
        raise UserAccountValidationError(
            "service status requires a service account user"
        )
    if row.is_service_account and status == USER_STATUS_ACTIVE:
        raise UserAccountValidationError(
            "service accounts must use service or disabled status"
        )


def _normalize_required_string(value: object, field_name: str) -> str:
    """Trim and require a non-empty string value for account inputs."""
    if not isinstance(value, str):
        raise UserAccountValidationError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise UserAccountValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_bounded_string(value: str, field_name: str, *, max_length: int) -> str:
    """Normalize a required string and enforce a maximum persisted length."""
    normalized = _normalize_required_string(value, field_name)
    if len(normalized) > max_length:
        raise UserAccountValidationError(
            f"{field_name} must be at most {max_length} characters"
        )
    return normalized


def _is_email_constraint_violation(exc: IntegrityError) -> bool:
    """Return whether an integrity error came from the normalized email index."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").lower()
    if constraint_name == USER_EMAIL_UNIQUE_CONSTRAINT:
        return True

    error_text = f"{exc.orig!s} {exc!s}".lower()
    return (
        USER_EMAIL_UNIQUE_CONSTRAINT in error_text
        or "unique constraint failed: index 'uq_users_email_lower'" in error_text
        or 'unique constraint failed: index "uq_users_email_lower"' in error_text
    )


def _is_retryable_user_storage_error(exc: SQLAlchemyError) -> bool:
    """Return whether a storage exception is safe to retry within the request."""
    if isinstance(exc, (DisconnectionError, OperationalError, SQLAlchemyTimeoutError)):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def _role_access_to_entry(
    row: UserRoleAssignmentORM, scope: AccessScopeORM
) -> UserRoleAssignmentEntry:
    """Map an active role assignment row into its public access-profile shape."""
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


def _permission_access_to_entry(
    row: UserPermissionGrantORM, scope: AccessScopeORM
) -> UserPermissionGrantEntry:
    """Map an active direct grant row into its public access-profile shape."""
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


def _next_user_cursor(items: tuple[UserAccountEntry, ...]) -> dict[str, str] | None:
    """Return the keyset cursor for continuing after the last returned account."""
    if not items:
        return None
    last_item = items[-1]
    return {"email": last_item.email, "id": last_item.id}
