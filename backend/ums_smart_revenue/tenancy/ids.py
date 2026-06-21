"""Tenant UUID parsing helpers shared by SQL-backed repositories."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

DEFAULT_TENANT_UUID: Final = UUID(UMS_TENANT_ID)


def resolve_repository_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit tenant input, request tenant context, or bootstrap tenant."""
    if tenant_id is None:
        current_tenant = get_current_tenant()
        return current_tenant.id if current_tenant is not None else DEFAULT_TENANT_UUID
    return parse_tenant_uuid(tenant_id)


def parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Parse tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    if not isinstance(tenant_id, str):
        raise ValueError("tenant_id must be a valid UUID")
    try:
        return UUID(tenant_id.strip())
    except ValueError as exc:
        raise ValueError("tenant_id must be a valid UUID") from exc
