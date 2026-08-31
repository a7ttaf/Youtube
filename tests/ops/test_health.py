# ============================================================================
# Purpose: Pin fail-closed readiness dependency handling and safe public errors.
# Database/ORM: SessionFactory is replaced with a test double; no database is
#   created or mutated.
# Standards: Driver details remain exception causes for diagnostics but never
#   become the public readiness message.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/ops/health.py -> subject.
#   - File: backend/ums_smart_revenue/app.py -> /readyz boundary.
# ============================================================================
"""Unit tests for the layer-appropriate readiness service."""

import pytest

from ums_smart_revenue.ops.health import (
    ReadinessUnavailableError,
    check_database_readiness,
)


def test_readiness_requires_a_configured_session_factory():
    """An app without a database is liveness-capable but not ready."""
    with pytest.raises(ReadinessUnavailableError, match="database is not configured"):
        check_database_readiness(None)


def test_readiness_hides_driver_details_from_public_error():
    """A connection failure is normalized without exposing its DSN/secret."""

    class _BrokenFactory:
        """Session factory double that fails before yielding a session."""

        def __call__(self):
            """Raise a driver-shaped error containing a secret locator."""
            raise RuntimeError("postgresql://user:TOPSECRET@db/app")

    with pytest.raises(ReadinessUnavailableError) as raised:
        check_database_readiness(_BrokenFactory())  # type: ignore[arg-type]

    assert str(raised.value) == "database is unavailable"
    assert "TOPSECRET" not in str(raised.value)
