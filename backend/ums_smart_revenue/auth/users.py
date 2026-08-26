# ============================================================================
# Purpose: Guarded user-account lifecycle repository — create, lookup, list,
#   and update tenant-scoped accounts with normalized inputs and typed domain
#   errors (conflict / not-found / validation / storage) at the API boundary.
# Database/ORM: UserORM (users) via an injected Session; every query is pinned
#   to the resolved tenant id.
# Standards: each storage attempt runs in its own SAVEPOINT so a failed flush
#   never discards a shared session's earlier writes; transient failures retry
#   once and everything else fails closed as UserAccountStorageError; an
#   invalidated connection is never retried at this granularity (the owning
#   transaction must retry).
# Blast Radius: Authorization (account lifecycle, service-account status) and
#   audit attribution. No finance math.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> SQLite BEGIN/SAVEPOINT
#     recipe the savepoint-per-attempt contract depends on.
#   - File: scripts/bootstrap_operator.py -> shared-session multi-account
#     create that must survive one account's failure.
#   - File: backend/ums_smart_revenue/api/users.py -> route layer mapping the
#     typed errors to HTTP statuses.
# ============================================================================
"""Tenant-scoped user account repository with typed lifecycle errors."""

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
from ums_smart_revenue.tenancy.context import TenantContextMissing, require_current_tenant

logger = logging.getLogger(__name__)
T = TypeVar("T")

USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUS_SERVICE = "service"
USER_STATUSES = frozenset({USER_STATUS_ACTIVE, USER_STATUS_DISABLED, USER_STATUS_SERVICE})
USER_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email_lower"
USER_EMAIL_MAX_LENGTH = 320
USER_DISPLAY_NAME_MAX_LENGTH = 200
USER_LIST_MAX_OFFSET = 10_000
_EMAIL_CONFLICT_SAMPLE_LIMIT = 2
USER_ACCOUNT_STORAGE_ATTEMPTS = 2


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
            "role_assignments": [assignment.to_api() for assignment in self.role_assignments],
            "direct_permissions": [grant.to_api() for grant in self.direct_permissions],
        }


@dataclass(frozen=True)
class _UserAccountUpdate:
    """Normalized optional account fields for one guarded update operation."""

    email: str | None
    display_name: str | None
    status: str | None


class UserAccountError(ValueError):
    """Base class for user account domain failures exposed to API handlers."""


class UserAccountConflictError(UserAccountError):
    """Raised when a requested account mutation conflicts with stored data."""


class UserAccountNotFoundError(UserAccountError):
    """Raised when a requested user account does not exist."""


class UserAccountValidationError(UserAccountError):
    """Raised when account input cannot be normalized into a safe value."""


class UserAccountStorageError(UserAccountError):
    """Raised when account storage is unavailable after retryable attempts."""


class UserAccountServiceAccountPolicyError(UserAccountError):
    """Raised when a stale service-account row blocks a non-owner update."""


class SqlAlchemyUserAccountRepository:
    """SQLAlchemy-backed repository for guarded user account lifecycle changes."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind repository operations to an explicit or current request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

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
        normalized_is_service_account = _normalize_service_account_flag(is_service_account)

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
                    USER_STATUS_SERVICE if normalized_is_service_account else USER_STATUS_ACTIVE
                ),
                is_service_account=normalized_is_service_account,
            )
            try:
                # FIX: The write runs in ITS OWN savepoint so the IntegrityError
                # diagnosis below can still query. A failed INSERT aborts the
                # PostgreSQL transaction and deactivates the session's current
                # (savepoint) transaction on every backend, so without this the
                # _email_exists SELECT raised PendingRollbackError and a typed
                # conflict surfaced as "storage unavailable". No full
                # session.rollback() here — multi-write callers (operator
                # bootstrap with repeated --email) share one session; a full
                # rollback discards earlier flushes while outcome lists still
                # report success.
                with self._session.begin_nested():
                    self._session.add(row)
                    self._session.flush()
            except IntegrityError as exc:
                if _is_email_constraint_violation(exc) or self._email_exists(normalized_email):
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

        def operation() -> tuple[tuple[UserAccountEntry, ...], bool, dict[str, str] | None]:
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
            rows = self._session.scalars(statement.limit(normalized_limit + 1)).all()
            items = tuple(self._to_entry(row) for row in rows[:normalized_limit])
            has_more = len(rows) > normalized_limit
            return (
                items,
                has_more,
                _next_user_cursor(items) if has_more else None,
            )

        return self._run_with_storage_retries(operation)

    # ============================================================================
    # Purpose: Resolve one account by its normalized email within the current
    #   tenant, returning ``None`` when no such account exists. Added so an
    #   operator bootstrap can be idempotent: ``create_user`` raises
    #   ``UserAccountConflictError`` on a re-run and the caller still has to
    #   report the SERVER-GENERATED id of the row that already exists (audit H3),
    #   which previously had no repository-owned lookup.
    # Database/ORM: UserORM/users — tenant-scoped read only.
    # Standards: Repository-owned SQLAlchemy read behind the same storage-retry
    #   wrapper as every other method here; the email is normalized through the
    #   shared ``_normalize_email`` so the lookup matches the ``uq_users_email_lower``
    #   index and the uniqueness rule ``create_user`` enforces. Absence is a
    #   ``None`` return, not an exception, because "not created yet" is the
    #   expected first-run state rather than a failure.
    # Blast Radius: Authorization-adjacent read. Grants nothing, mutates nothing,
    #   and cannot cross a tenant boundary (``tenant_id`` is always in the WHERE).
    # Connections:
    #   - File: scripts/bootstrap_operator.py -> the idempotent first-run caller.
    #   - File: backend/ums_smart_revenue/auth/users.py -> ``_email_exists`` uses
    #     the identical normalized-email predicate for the conflict check.
    # ============================================================================
    def get_user_by_email(self, *, email: str) -> UserAccountEntry | None:
        """Return the tenant's account for ``email``, or ``None`` when absent."""
        normalized_email = _normalize_email(email)

        def operation() -> UserAccountEntry | None:
            """Attempt one normalized-email account lookup against the session."""
            row = self._session.scalars(
                select(UserORM)
                .where(
                    UserORM.tenant_id == self._tenant_id,
                    func.lower(UserORM.email) == normalized_email,
                )
                .order_by(UserORM.id)
                .limit(1)
            ).one_or_none()
            return None if row is None else self._to_entry(row)

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
                    _permission_access_to_entry(row, scope) for row, scope in permission_rows
                ),
            )

        return self._run_with_storage_retries(operation)

    # ============================================================================
    # Purpose: Apply guarded account metadata and lifecycle updates for one tenant.
    # Database/ORM: UserORM/users.
    # Standards: Repository-owned write, typed domain errors, savepoint-scoped
    #   rollback on conflicts (sibling writes on a shared session are preserved).
    # Blast Radius: Authorization and audit-adjacent account lifecycle state.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/user_accounts.py -> Route error mapping.
    #   - File: backend/ums_smart_revenue/auth/user_auth_service.py -> Principal loading.
    # ============================================================================
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
        update = _normalize_user_account_update(
            email=email,
            display_name=display_name,
            status=status,
        )

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

            if update.email is not None and (
                self._email_exists(update.email, excluding_user_id=user_uuid)
            ):
                raise UserAccountConflictError("User email already exists")

            if update.status is not None:
                _require_compatible_status(row, update.status)

            try:
                # FIX: Same savepoint isolation as create_user — the failed
                # UPDATE must be rolled back to a savepoint BEFORE the email
                # diagnosis below queries, or the deactivated transaction turns
                # the typed conflict into "storage unavailable". Sibling writes
                # on the shared session stay flushed either way.
                with self._session.begin_nested():
                    _apply_user_account_update(row, update)
                    self._session.flush()
            except IntegrityError as exc:
                if update.email is not None and (
                    _is_email_constraint_violation(exc)
                    or self._email_exists(update.email, excluding_user_id=user_uuid)
                ):
                    raise UserAccountConflictError("User email already exists") from exc
                raise UserAccountConflictError(
                    "User account violates database constraints"
                ) from exc
            return self._to_entry(row)

        return self._run_with_storage_retries(operation)

    # ========================================================================
    # Purpose: Retry transient storage failures inside a SAVEPOINT so a failed
    #   flush rolls back only that attempt; sibling writes on a shared session
    #   (operator bootstrap multi-account) stay flushed. Integrity conflicts stay
    #   typed — never remapped to "storage unavailable".
    # Database/ORM: UserORM/users via the caller's Session; begin_nested savepoints.
    # Standards: Repository-owned retry; typed UserAccountConflictError /
    #   UserAccountStorageError; no full session.rollback(); SQLite depends on
    #   db/session.py BEGIN recipe so RELEASE does not early-commit.
    # Blast Radius: Authorization account lifecycle writes and bootstrap atomicity.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/session.py -> SQLite BEGIN/SAVEPOINT.
    #   - File: scripts/bootstrap_operator.py -> shared-session multi-account create.
    # ========================================================================
    def _run_with_storage_retries(self, operation: Callable[[], T]) -> T:
        """Retry transient storage failures once and fail closed otherwise.

        Each attempt runs inside ``begin_nested()`` so a failed flush rolls
        back only that savepoint. A full ``session.rollback()`` would discard
        earlier writes in a shared multi-account bootstrap transaction.

        On SQLite this depends on the engine-level BEGIN recipe in
        ``db/session.py::build_engine``: without a real outer transaction,
        pysqlite's RELEASE of the outermost savepoint durably COMMITS, which
        is an early commit that breaks the caller's one-transaction envelope.
        """
        for attempt_index in range(USER_ACCOUNT_STORAGE_ATTEMPTS):
            try:
                with self._session.begin_nested():
                    return operation()
            except IntegrityError as exc:
                # FIX: A constraint violation is deterministic — never retryable
                # storage trouble. create_user/update_user diagnose their own
                # flush failures (email vs other) inside their inner savepoint,
                # so an IntegrityError surfacing HERE was raised by the savepoint
                # boundary itself flushing prior pending session state
                # (SessionTransaction._take_snapshot flushes on entry). The
                # savepoint rework mapped that onto "storage unavailable",
                # misreporting a typed conflict; keep the typed contract.
                raise UserAccountConflictError(
                    "User account violates database constraints"
                ) from exc
            except SQLAlchemyError as exc:
                # UserAccountConflictError is a ValueError subclass (not
                # SQLAlchemy), so typed conflicts propagate without a useless
                # `except: raise` (PYL-W0706).
                # Nested savepoint already rolled back on exit from begin_nested.
                if (
                    attempt_index + 1 >= USER_ACCOUNT_STORAGE_ATTEMPTS
                    or not _is_retryable_user_storage_error(exc)
                ):
                    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
                        # The dead connection left the transaction unusable;
                        # roll back so the caller's error handling can still
                        # use the session after catching the typed error.
                        self._session.rollback()
                    raise UserAccountStorageError("User account storage unavailable") from exc
                logger.warning("Retrying user account storage operation after transient failure")
        raise RuntimeError("unreachable user account retry state")

    def _email_exists(self, email: str, *, excluding_user_id: UUID | None = None) -> bool:
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


# ============================================================================
# Purpose: Normalize and apply optional user-account update fields consistently.
# Database/ORM: UserORM/users.
# Standards: Repository helper, typed validation errors, no direct API concerns.
# Blast Radius: Authorization and audit-adjacent account lifecycle state.
# Connections:
#   - File: backend/ums_smart_revenue/auth/users.py -> SqlAlchemyUserAccountRepository.update_user.
#   - File: backend/ums_smart_revenue/db/security_models.py -> UserORM field contract.
# ============================================================================
def _normalize_user_account_update(
    *,
    email: str | None,
    display_name: str | None,
    status: str | None,
) -> _UserAccountUpdate:
    """Normalize optional account update fields before opening the write attempt."""
    return _UserAccountUpdate(
        email=_normalize_email(email) if email is not None else None,
        display_name=(
            _normalize_bounded_string(
                display_name,
                "display_name",
                max_length=USER_DISPLAY_NAME_MAX_LENGTH,
            )
            if display_name is not None
            else None
        ),
        status=_normalize_status(status) if status is not None else None,
    )


def _apply_user_account_update(row: UserORM, update: _UserAccountUpdate) -> None:
    """Apply only fields explicitly present in the normalized update payload."""
    if update.email is not None:
        row.email = update.email
    if update.display_name is not None:
        row.display_name = update.display_name
    if update.status is not None:
        row.status = update.status


def _normalize_email(value: str) -> str:
    """Normalize an account email and reject malformed local or domain parts."""
    normalized = _normalize_bounded_string(value, "email", max_length=USER_EMAIL_MAX_LENGTH).lower()
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
        raise UserAccountValidationError(f"Unknown status: {normalized}; allowed: {allowed}")
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
        raise UserAccountValidationError("cursor_email and cursor_id must be provided together")
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
        raise UserAccountValidationError("service status requires a service account user")
    if row.is_service_account and status == USER_STATUS_ACTIVE:
        raise UserAccountValidationError("service accounts must use service or disabled status")


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
        raise UserAccountValidationError(f"{field_name} must be at most {max_length} characters")
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


# ============================================================================
# Purpose: Classify which SQLAlchemy storage failures may be retried inside
#   ``UserAccountRepository._run_with_storage_retries`` (savepoint granularity).
# Database/ORM: None directly; consults ``DBAPIError.connection_invalidated`` and
#   transient OperationalError / DisconnectionError / TimeoutError types.
# Standards: Fail closed — ``connection_invalidated`` is NEVER retryable here
#   because the Session/transaction envelope is dead; only the owning caller
#   (e.g. bootstrap) may open a fresh session. Integrity conflicts are handled
#   before this helper is consulted.
# Blast Radius: Authorization account writes; incorrect True would retry on a
#   dead connection and mis-report storage availability.
# Connections:
#   - File: backend/ums_smart_revenue/auth/users.py -> ``_run_with_storage_retries``.
#   - File: scripts/bootstrap_operator.py -> owning transaction that must retry.
# ============================================================================
def _is_retryable_user_storage_error(exc: SQLAlchemyError) -> bool:
    """Return whether a storage exception is safe to retry within the request."""
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        # FIX: a lost connection discards the caller's whole transaction
        # envelope — including earlier flushed writes in a shared bootstrap
        # session — so a savepoint-granular retry would silently continue
        # without them. Never retryable here; only the owning transaction can
        # retry safely. (Checked first: OperationalError is a DBAPIError
        # subclass and can carry connection_invalidated too.)
        return False
    return isinstance(exc, (DisconnectionError, OperationalError, SQLAlchemyTimeoutError))


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve an explicit or request-scoped account tenant id."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    try:
        return require_current_tenant().id
    except TenantContextMissing as exc:
        raise UserAccountValidationError("tenant context is required") from exc


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise UserAccountValidationError("tenant_id must be a valid UUID") from exc


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
