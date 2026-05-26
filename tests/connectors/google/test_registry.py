"""Connector registry: maps --connector key to a runner callable."""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.registry import (
    dispatch_connector,
    register_connector,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    from ums_smart_revenue.connectors.google import registry
    snap = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(snap)


def test_register_and_dispatch() -> None:
    def runner(**kwargs):
        return "ok"
    register_connector(key="youtube-reporting", runner=runner)
    assert dispatch_connector(key="youtube-reporting") is runner


def test_dispatch_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown connector key"):
        dispatch_connector(key="no-such")
