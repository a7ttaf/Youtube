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

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus


class TenantNotFoundError(LookupError):
    """Raised when a tenant lookup by slug or id returns no row."""


class TenantValidationError(ValueError):
    """Raised when an input to the repository fails normalisation.

    Kept separate from :class:`TenantNotFoundError` so the resolver
    middleware can map blank-or-malformed slugs to a 400 rather than 404.
    """


class TenantRepository(Protocol):
    """Read interface required by :class:`TenantResolverMiddleware` and friends."""

    def get_by_slug(self, slug: str) -> Tenant: ...
    def get_by_id(self, tenant_id: UUID) -> Tenant: ...


class SqlAlchemyTenantRepository:
    """Production :class:`TenantRepository` over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_slug(self, slug: str) -> Tenant:
        normalised = _normalise_slug(slug)
        row = self._session.scalars(
            select(TenantORM).where(TenantORM.slug == normalised)
        ).one_or_none()
        if row is None:
            raise TenantNotFoundError(f"No tenant with slug {normalised!r}")
        return _to_domain(row)

    def get_by_id(self, tenant_id: UUID) -> Tenant:
        row = self._session.get(TenantORM, tenant_id)
        if row is None:
            raise TenantNotFoundError(f"No tenant with id {tenant_id}")
        return _to_domain(row)


def _normalise_slug(slug: str) -> str:
    if not isinstance(slug, str):
        raise TenantValidationError("Tenant slug must be a string")
    normalised = slug.strip().lower()
    if not normalised:
        raise TenantValidationError("Tenant slug must not be blank")
    return normalised


def _to_domain(row: TenantORM) -> Tenant:
    return Tenant(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name,
        primary_currency=row.primary_currency,
        status=TenantStatus(row.status),
        onboarding_at=row.onboarding_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
