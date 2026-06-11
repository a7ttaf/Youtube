"""Unit tests for ``connector_tenant_context``.

The CLI (and, later, the executor worker) drives ``run_one`` outside any
FastAPI request, so the per-transaction RLS session hook
(``db/session.py:_apply_tenant_isolation``) finds no ``TENANT_CTX`` and pins the
restricted ``app_tenant`` lane with NO trusted tenant row -> every tenant-table
policy denies all rows and the run dies fail-closed at the credential read.

``connector_tenant_context(tenant_id)`` sets ``TENANT_CTX`` to the minimal
``Tenant`` the hook needs (it reads only ``tenant.id``) for the duration of the
block and resets the contextvar token on exit, even when the body raises. These
tests pin that contract without any database.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.tenancy.context import TENANT_CTX, get_current_tenant

_TENANT_ID = UUID("00000000-0000-0000-0000-0000009c0001")


def test_sets_current_tenant_id_inside_block() -> None:
    """Inside the block the active tenant carries the supplied id."""
    assert get_current_tenant() is None
    with connector_tenant_context(_TENANT_ID):
        tenant = get_current_tenant()
        assert tenant is not None
        assert tenant.id == _TENANT_ID
    assert get_current_tenant() is None


def test_resets_context_on_exception() -> None:
    """An exception inside the block still resets ``TENANT_CTX`` to its prior value."""
    assert get_current_tenant() is None
    with pytest.raises(ValueError, match="boom"):
        with connector_tenant_context(_TENANT_ID):
            assert get_current_tenant() is not None
            raise ValueError("boom")
    assert get_current_tenant() is None


def test_restores_prior_tenant_value() -> None:
    """The token reset restores a previously-set tenant, not just ``None``."""
    other_id = UUID("00000000-0000-0000-0000-0000009c0002")
    token = TENANT_CTX.set(
        # Reuse the helper to build a prior tenant so the test stays
        # independent of the full Tenant constructor surface.
        _build_prior_tenant(other_id)
    )
    try:
        with connector_tenant_context(_TENANT_ID):
            assert get_current_tenant().id == _TENANT_ID
        restored = get_current_tenant()
        assert restored is not None
        assert restored.id == other_id
    finally:
        TENANT_CTX.reset(token)


def _build_prior_tenant(tenant_id: UUID):
    """Build a stand-in prior tenant via the same helper under test."""
    with connector_tenant_context(tenant_id):
        return get_current_tenant()
