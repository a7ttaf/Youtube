# ============================================================================
# Purpose: Shared httpx2 MockTransport helpers for Google connector client tests.
# Database/ORM: None.
# Standards: httpx2 imported as httpx; pytest fixtures only.
# Blast Radius: Test harness for B2.4+ connector client suites.
# Connections:
#   - File: tests/connectors/google/test_http_client.py -> Primary consumer.
# ============================================================================
"""Shared httpx.MockTransport helpers for B2.4+ client tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest


def make_mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_credentials():
    """Build a stub google-auth Credentials that no-ops before_request."""

    class _StubCreds:
        token = "fake-bearer"

        def before_request(self, request: Any, method: str, url: str, headers: dict) -> None:
            headers["Authorization"] = f"Bearer {self.token}"

    return _StubCreds()
