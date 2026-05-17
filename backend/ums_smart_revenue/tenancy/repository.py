"""Read-side repository for the ``tenants`` table.

Only the lookups the resolver needs are exposed here:

* :meth:`TenantRepository.get_by_slug` — primary lookup, used per request.
* :meth:`TenantRepository.get_by_id`   — used by background workers and
  platform-admin endpoints that already hold a UUID.

Mutations (create / suspend / archive) live in the platform-admin write
surface added in a later S2 slice.

The ``Protocol`` form makes it trivial to substitute an in-memory fake
in tests or a Redis-cached wrapper later. The SQLAlchemy implementation
is the production default.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_TENANT_SLUG_LENGTH = 255


class TenantNotFoundError(LookupError):
    """Raised when a tenant lookup by slug or id returns no row."""


class TenantValidationError(ValueError):
    """Raised when an input to the repository fails normalisation.

    Kept separate from :class:`TenantNotFoundError` so the resolver
    middleware can map blank-or-malformed slugs to a 400 rather than 404.
    """


class TenantRepository(Protocol):
    """Read interface required by :class:`TenantResolverMiddleware` and friends."""

    def get_by_slug(self, slug: str) -> Tenant:
        """Return a tenant by slug after applying repository normalization."""
        ...

    def get_by_id(self, tenant_id: UUID) -> Tenant:
        """Return a tenant by stable UUID."""
        ...


class SqlAlchemyTenantRepository:
    """Production :class:`TenantRepository` over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to the caller-owned SQLAlchemy session."""
        self._session = session

    def get_by_slug(self, slug: str) -> Tenant:
        """Return the tenant identified by a normalized slug or raise if absent."""
        normalised = _normalise_slug(slug)
        row = self._session.scalars(
            select(TenantORM).where(TenantORM.slug == normalised)
        ).one_or_none()
        if row is None:
            raise TenantNotFoundError(f"No tenant with slug {normalised!r}")
        return _to_domain(row)

    def get_by_id(self, tenant_id: UUID) -> Tenant:
        """Return the tenant identified by UUID or raise if absent."""
        row = self._session.get(TenantORM, tenant_id)
        if row is None:
            raise TenantNotFoundError(f"No tenant with id {tenant_id}")
        return _to_domain(row)


def _normalise_slug(slug: str) -> str:
    """Trim and lowercase tenant slugs while rejecting blank input."""
    if not isinstance(slug, str):
        raise TenantValidationError("Tenant slug must be a string")
    normalised = slug.strip().lower()
    if not normalised:
        raise TenantValidationError("Tenant slug must not be blank")
    if len(normalised) > MAX_TENANT_SLUG_LENGTH:
        raise TenantValidationError(
            f"Tenant slug must be at most {MAX_TENANT_SLUG_LENGTH} characters"
        )
    return normalised


def _to_domain(row: TenantORM) -> Tenant:
    """Convert an ORM tenant row into a validated immutable domain object."""
    tenant_id = _require_uuid(row, "id")
    slug = _require_nonblank_string(row, "slug")
    display_name = _require_nonblank_string(row, "display_name")
    onboarding_at = _coerce_aware_datetime(row, "onboarding_at")
    created_at = _coerce_aware_datetime(row, "created_at")
    updated_at = _coerce_aware_datetime(row, "updated_at")
    primary_currency = _validate_currency(row)
    try:
        status = TenantStatus(row.status)
    except ValueError as exc:
        raise ValueError(f"invalid tenant status: {row.status!r}") from exc

    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=display_name,
        primary_currency=primary_currency,
        status=status,
        onboarding_at=onboarding_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _require_uuid(row: TenantORM, field_name: str) -> UUID:
    """Return a required UUID field or raise a validation error."""
    value = getattr(row, field_name)
    if not isinstance(value, UUID):
        raise ValueError(f"invalid {field_name}: must be a UUID")
    return value


def _require_nonblank_string(row: TenantORM, field_name: str) -> str:
    """Return a required text field or raise a validation error."""
    value = getattr(row, field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field_name}: must be a non-empty string")
    return value


def _coerce_aware_datetime(row: TenantORM, field_name: str) -> datetime:
    """Return a UTC-aware datetime, repairing SQLite test rows only."""
    value = getattr(row, field_name)
    if value is None:
        raise ValueError(f"invalid timezone for {field_name}")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)

    session = object_session(row)
    bind = session.get_bind() if session is not None else None
    if bind is not None and bind.dialect.name == "sqlite":
        # SQLite strips tzinfo from aware SQLAlchemy DateTime values.
        return value.replace(tzinfo=UTC)

    raise ValueError(f"invalid timezone for {field_name}")


def _validate_currency(row: TenantORM) -> str:
    """Return the ISO 4217 currency code or reject malformed values."""
    value = row.primary_currency
    if not isinstance(value, str) or CURRENCY_RE.fullmatch(value) is None:
        raise ValueError("invalid primary_currency: must be 3 uppercase letters")
    return value
