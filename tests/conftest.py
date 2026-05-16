"""Shared pytest configuration for the UMS Smart Revenue test suite.

This conftest is loaded automatically by pytest. It performs two duties:

1. Makes the ``backend/`` source tree importable as ``ums_smart_revenue``.
2. Provides a deterministic, **test-only** value for
   ``UMS_TRUSTED_GATEWAY_TOKEN`` when the surrounding environment has not
   provided one.
   Real secrets must never be committed to the repository (see SECURITY.md).
   Tests that exercise authorization can use this sentinel while CI remains
   free to inject the value from its secret store.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
_backend_path = str(BACKEND_PATH)
if _backend_path in sys.path:
    sys.path.remove(_backend_path)
sys.path.insert(0, _backend_path)

TEST_TRUSTED_GATEWAY_TOKEN_ENV = "UMS_TRUSTED_GATEWAY_TOKEN"
# Sentinel value used only when running tests. The prefix makes any leak
# into production logs immediately recognisable as test scaffolding, not
# a credential. The literal must stay stable so test fixtures that build
# request headers can match against it. Not a real secret.
_TEST_TOKEN_SENTINEL = "pytest-trusted-gateway-token"  # noqa: S105 — test scaffold


def _ensure_test_gateway_token() -> None:
    """Provide a deterministic pytest sentinel when no token is configured.

    This conftest only runs under pytest, so local tests get a stable fallback
    while CI or operator-provided environment values remain authoritative.
    """
    if not os.environ.get(TEST_TRUSTED_GATEWAY_TOKEN_ENV):
        os.environ[TEST_TRUSTED_GATEWAY_TOKEN_ENV] = _TEST_TOKEN_SENTINEL


_ensure_test_gateway_token()


@pytest.fixture(autouse=True)
def reset_app_settings_cache():
    """Clear the cached app settings around each test so env changes apply."""
    from ums_smart_revenue.config.settings import load_app_settings

    load_app_settings.cache_clear()
    yield
    load_app_settings.cache_clear()
