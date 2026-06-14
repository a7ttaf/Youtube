"""Unit tests for ``tenancy.models`` domain factories.

The placeholder factory is the shared seam used by both the test-only
branch of ``connectors/runs/tenant_context.py`` and the Bucket-A
``_audit_failed_before_start`` audit path in ``connectors/runs/executor.py``.
Pinning its contract here means a drift in the placeholder shape will
fail locally instead of silently changing what the RLS session hook sees
in two different call sites.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ums_smart_revenue.tenancy.models import (
    DEFAULT_PRIMARY_CURRENCY,
    PLACEHOLDER_TENANT_EPOCH,
    Tenant,
    TenantStatus,
    make_placeholder_tenant,
)


def test_make_placeholder_tenant_returns_active_tenant_with_supplied_id() -> None:
    """The factory returns an ACTIVE ``Tenant`` carrying the supplied id."""
    tenant_id = uuid4()
    tenant = make_placeholder_tenant(
        tenant_id=tenant_id,
        slug="placeholder-slug",
        display_name="placeholder display",
    )
    assert isinstance(tenant, Tenant)
    assert tenant.id == tenant_id
    assert tenant.slug == "placeholder-slug"
    assert tenant.display_name == "placeholder display"
    assert tenant.status == TenantStatus.ACTIVE
    assert tenant.is_active is True


def test_make_placeholder_tenant_uses_shared_epoch_sentinel() -> None:
    """Every timestamp field uses the shared ``PLACEHOLDER_TENANT_EPOCH``."""
    tenant_id = uuid4()
    tenant = make_placeholder_tenant(
        tenant_id=tenant_id,
        slug="placeholder-slug",
        display_name="placeholder display",
    )
    assert tenant.onboarding_at == PLACEHOLDER_TENANT_EPOCH
    assert tenant.created_at == PLACEHOLDER_TENANT_EPOCH
    assert tenant.updated_at == PLACEHOLDER_TENANT_EPOCH
    # The sentinel must be timezone-aware UTC so it satisfies the schema's
    # ``DateTime(timezone=True)`` contract should the placeholder ever
    # reach a comparison against a real row.
    assert tenant.onboarding_at.tzinfo is UTC


def test_make_placeholder_tenant_defaults_to_usd() -> None:
    """The default primary currency is the module-level constant (``USD``)."""
    tenant = make_placeholder_tenant(
        tenant_id=uuid4(),
        slug="placeholder-slug",
        display_name="placeholder display",
    )
    assert tenant.primary_currency == DEFAULT_PRIMARY_CURRENCY
    assert tenant.primary_currency == "USD"


def test_make_placeholder_tenant_honours_currency_override() -> None:
    """A non-default currency overrides the ``USD`` fallback."""
    tenant = make_placeholder_tenant(
        tenant_id=uuid4(),
        slug="placeholder-slug",
        display_name="placeholder display",
        primary_currency="EUR",
    )
    assert tenant.primary_currency == "EUR"


def test_placeholder_tenant_epoch_is_utc_1970() -> None:
    """The shared sentinel is the deterministic UTC 1970 epoch."""
    assert PLACEHOLDER_TENANT_EPOCH == datetime(1970, 1, 1, tzinfo=UTC)
