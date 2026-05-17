"""Platform-admin identity, status, and loader.

Platform admins are deliberately stored in their own ``platform_admins``
table (introduced in S2.1) — they are never rows in ``users`` and never
hold tenant-scoped roles. Keeping the data shape separate makes it
impossible to *accidentally* assign tenant-bound resources to a
platform admin: there is no foreign-key surface to do so.

S2.3 publishes:

* :class:`PlatformAdminStatus` mirroring the SQL CHECK constraint.
* :class:`PlatformAdminPrincipal`, the immutable in-process view used
  by request handlers and the future platform-admin policy helpers.
* :class:`SqlAlchemyPlatformAdminLoader` for loading a principal from
  the trusted gateway's admin id, with the same fail-closed semantics
  as :class:`SqlAlchemyPrincipalLoader` (disabled / not-found are loud).
* :data:`Principal` — the union type any code that legitimately accepts
  both tenant users and platform admins should annotate with.

Routes today still take :class:`UserPrincipal` directly. The
``Principal`` alias is here for the platform-admin endpoints that land
later in S2; tenant-scoped routes continue to type as
``UserPrincipal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.db.tenant_models import PlatformAdminORM

PLATFORM_ADMIN_QUERY_TIMEOUT_MS = 5_000


class PlatformAdminStatus(StrEnum):
    """Lifecycle states for a platform admin — mirrors the SQL CHECK constraint."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class PlatformAdminPrincipal:
    """Immutable view of a row in ``platform_admins``.

    Carries identity and lifecycle status — nothing else. Platform-level
    capabilities (creating tenants, reading platform audit logs) are
    derived from this type alone; there is no role/permission lookup.
    """

    admin_id: str
    email: str
    status: PlatformAdminStatus = PlatformAdminStatus.ACTIVE

    @property
    def is_active(self) -> bool:
        """Return whether this principal may act as a platform admin."""
        return self.status == PlatformAdminStatus.ACTIVE


# The union any code that legitimately handles both kinds of caller
# should accept. Tenant-scoped routes keep typing as UserPrincipal so
# the type checker rejects accidental platform-admin reuse.
Principal = UserPrincipal | PlatformAdminPrincipal


class PlatformAdminLoadError(ValueError):
    """Base error for platform-admin principal loading failures."""


class PlatformAdminNotFoundError(PlatformAdminLoadError):
    """Raised when the trusted admin id does not map to a stored row."""


class PlatformAdminDisabledError(PlatformAdminLoadError):
    """Raised when the platform admin exists but cannot authenticate."""


class PlatformAdminValidationError(PlatformAdminLoadError):
    """Raised when the trusted admin id cannot be parsed as a UUID."""


class PlatformAdminDataValidationError(PlatformAdminLoadError):
    """Raised when a stored platform-admin row cannot be parsed."""


class PlatformAdminTransactionError(PlatformAdminLoadError):
    """Raised when a caller transaction prevents read isolation setup."""


class SqlAlchemyPlatformAdminLoader:
    """Load platform-admin principals from SQL state.

    Mirrors :class:`SqlAlchemyPrincipalLoader` in shape so the API
    dependency layer can route between the two by request path or by
    the upstream identity gateway's claim type.
    """

    def __init__(self, session: Session) -> None:
        """Store the SQLAlchemy session used for one admin lookup."""
        self._session = session

    def load(self, *, admin_id: str) -> PlatformAdminPrincipal:
        """Return a :class:`PlatformAdminPrincipal` or raise loudly."""
        parsed_id = _parse_uuid(admin_id)
        if self._session.in_transaction():
            raise PlatformAdminTransactionError(
                "Platform-admin loads require a session without an active transaction"
            )
        with self._session.begin():
            self._prepare_read_connection()
            return self._load_principal(parsed_id)

    def _load_principal(self, parsed_id: UUID) -> PlatformAdminPrincipal:
        """Load and validate a platform-admin row inside the read transaction."""
        row = self._session.get(PlatformAdminORM, parsed_id)
        if row is None:
            raise PlatformAdminNotFoundError("Platform admin is not registered")
        try:
            status = PlatformAdminStatus(row.status)
        except ValueError as exc:
            raise PlatformAdminDataValidationError(
                f"Unknown platform admin status in database: {row.status}"
            ) from exc
        if status != PlatformAdminStatus.ACTIVE:
            raise PlatformAdminDisabledError(
                f"Platform admin status {status.value!r} is not authoritative"
            )
        return PlatformAdminPrincipal(
            admin_id=str(row.id),
            email=row.email,
            status=status,
        )

    def _prepare_read_connection(self) -> None:
        """Apply principal read isolation and timeout settings to the transaction."""
        bind = self._session.get_bind()
        connection = self._session.connection(
            execution_options={"isolation_level": "SERIALIZABLE"}
        )
        if bind.dialect.name == "postgresql":
            connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {PLATFORM_ADMIN_QUERY_TIMEOUT_MS}"
            )


def _parse_uuid(value: str) -> UUID:
    """Parse trusted platform-admin input into a UUID."""
    if not isinstance(value, str):
        raise PlatformAdminValidationError("admin_id must be a string")
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise PlatformAdminValidationError("admin_id must be a valid UUID") from exc
