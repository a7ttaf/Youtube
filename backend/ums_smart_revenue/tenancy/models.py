"""Tenancy domain model — frozen dataclasses for in-process use.

These are separate from the SQLAlchemy ORM in
``ums_smart_revenue.db.tenant_models`` on purpose:

* ORM objects are tied to a session lifetime and lazy-load attributes;
  passing them across request scopes or into background workers is a
  classic source of detached-instance bugs.
* The domain dataclass is immutable, easy to compare, safe to log, and
  trivial to hold inside a ``contextvars.ContextVar``.

Conversion happens at the repository boundary (see ``repository.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class TenantStatus(str, Enum):
    """Lifecycle states for a tenant — mirrors the SQL CHECK constraint."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class Tenant:
    """Immutable view of a row in the ``tenants`` table.

    All datetime fields are timezone-aware (the schema declares them with
    ``DateTime(timezone=True)``). Currency codes are uppercase three-letter
    ISO 4217 strings.
    """

    id: UUID
    slug: str
    display_name: str
    primary_currency: str
    status: TenantStatus
    onboarding_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        """Convenience flag for the most common decision the API will make."""
        return self.status == TenantStatus.ACTIVE
