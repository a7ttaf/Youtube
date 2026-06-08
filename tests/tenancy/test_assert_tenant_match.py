from uuid import UUID

import pytest

from ums_smart_revenue.tenancy.isolation import (
    TenantIsolationError,
    assert_tenant_match,
)

A = UUID("00000000-0000-0000-0000-000000000001")
B = UUID("00000000-0000-0000-0000-000000000002")


def test_match_passes_for_equal_uuids():
    """Verify equal tenant IDs are accepted."""
    assert_tenant_match(A, A)  # no raise


def test_match_accepts_string_forms():
    """Verify mixed UUID string/object forms are normalized."""
    assert_tenant_match(str(A), A)
    assert_tenant_match(A, str(A))


def test_mismatch_raises():
    """Verify mismatched tenant IDs fail closed."""
    with pytest.raises(TenantIsolationError):
        assert_tenant_match(A, B)


def test_none_raises():
    """Verify missing tenant IDs fail closed."""
    with pytest.raises(TenantIsolationError):
        assert_tenant_match(None, A)
