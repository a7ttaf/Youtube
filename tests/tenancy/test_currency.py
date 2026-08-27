# ============================================================================
# Purpose: Pin the two behaviours of get_tenant_primary_currency — it returns
#   the ACTIVE tenant's declared primary_currency (never a constant, never the
#   settings default), and it fails closed with the existing typed
#   TenantContextMissing when no tenant is bound.
# Database/ORM: None — the helper is a contextvar read; these tests set
#   TENANT_CTX directly rather than going through the resolver.
# Standards: Every contextvar set is reset in a finally so no tenant leaks into
#   a sibling test. Codes used are deliberately NOT the "USD" default, so a
#   hardcoded-literal regression cannot pass.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/currency.py -> helper under test.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> TENANT_CTX.
# ============================================================================
"""Behaviour tests for the tenant primary-currency accessor."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ums_smart_revenue.tenancy.context import TENANT_CTX, TenantContextMissing
from ums_smart_revenue.tenancy.currency import get_tenant_primary_currency
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus


def _make_tenant(primary_currency: str) -> Tenant:
    """Build an immutable tenant carrying *primary_currency* for context tests."""
    now = datetime.now(UTC)
    return Tenant(
        id=uuid4(),
        slug="ums",
        display_name="UMS",
        primary_currency=primary_currency,
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("code", ["EGP", "AED", "USD"])
def test_returns_the_context_tenants_primary_currency(code: str) -> None:
    """The helper reports whatever currency the bound tenant declares."""
    token = TENANT_CTX.set(_make_tenant(code))
    try:
        assert get_tenant_primary_currency() == code
    finally:
        TENANT_CTX.reset(token)


def test_reads_the_innermost_bound_tenant() -> None:
    """A nested tenant binding wins while it is active, then unwinds."""
    outer = TENANT_CTX.set(_make_tenant("EGP"))
    try:
        inner = TENANT_CTX.set(_make_tenant("AED"))
        try:
            assert get_tenant_primary_currency() == "AED"
        finally:
            TENANT_CTX.reset(inner)
        assert get_tenant_primary_currency() == "EGP"
    finally:
        TENANT_CTX.reset(outer)


def test_fails_closed_without_tenant_context() -> None:
    """No tenant in context raises the typed error — there is no default currency."""
    assert TENANT_CTX.get() is None
    with pytest.raises(TenantContextMissing):
        get_tenant_primary_currency()
