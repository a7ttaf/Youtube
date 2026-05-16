"""Behaviour tests for the tenant contextvar."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ums_smart_revenue.tenancy.context import (
    TENANT_CTX,
    TenantContextMissing,
    get_current_tenant,
    require_current_tenant,
)
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus


def _make_tenant(slug: str = "ums") -> Tenant:
    now = datetime.now(timezone.utc)
    return Tenant(
        id=uuid4(),
        slug=slug,
        display_name=slug.upper(),
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def test_default_context_is_none():
    assert get_current_tenant() is None


def test_require_current_tenant_raises_when_unset():
    with pytest.raises(TenantContextMissing):
        require_current_tenant()


def test_set_and_get_round_trip():
    tenant = _make_tenant()
    token = TENANT_CTX.set(tenant)
    try:
        assert get_current_tenant() is tenant
        assert require_current_tenant() is tenant
    finally:
        TENANT_CTX.reset(token)


def test_reset_restores_previous_value():
    first = _make_tenant("ums")
    second = _make_tenant("rotana")

    outer_token = TENANT_CTX.set(first)
    inner_token = TENANT_CTX.set(second)
    try:
        assert get_current_tenant() is second
    finally:
        TENANT_CTX.reset(inner_token)
    assert get_current_tenant() is first
    TENANT_CTX.reset(outer_token)
    assert get_current_tenant() is None


def test_distinct_contexts_do_not_bleed():
    """Each :class:`contextvars.Context` carries its own value."""
    import contextvars

    tenant = _make_tenant("ums")

    captured = {}

    def inner() -> None:
        captured["inside"] = get_current_tenant()

    outer_ctx = contextvars.copy_context()
    outer_ctx.run(inner)
    assert captured["inside"] is None

    token = TENANT_CTX.set(tenant)
    try:
        ctx = contextvars.copy_context()
        ctx.run(inner)
        assert captured["inside"] is tenant
    finally:
        TENANT_CTX.reset(token)

    # After reset the snapshot context still sees the value it captured.
    ctx.run(inner)
    assert captured["inside"] is tenant
