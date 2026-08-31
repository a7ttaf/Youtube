# ============================================================================
# Purpose: Pin Compose log-level propagation and the graceful-shutdown budget.
# Database/ORM: None.
# Standards: Static YAML validation against runtime-owned timeout constants.
# Blast Radius: Runtime logging and connector shutdown durability.
# Connections:
#   - File: docker-compose.yml -> Shared environment and stop grace periods.
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> Scheduler
#     close join budget.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> Executor
#     close drain budget.
# ============================================================================
"""Static contracts for the local Compose runtime configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

from ums_smart_revenue.connectors.runs import executor, scheduler

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _PROJECT_ROOT / "docker-compose.yml"
_LOG_LEVEL_EXPRESSION = "${UMS_LOG_LEVEL:-INFO}"


def _load_compose() -> dict[str, object]:
    """Load Compose YAML with anchors and merge keys resolved."""
    loaded = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_app_services_inherit_default_log_level() -> None:
    """Every Python service inherits the operator-overridable INFO default."""
    compose = _load_compose()
    assert compose["x-app-env"]["UMS_LOG_LEVEL"] == _LOG_LEVEL_EXPRESSION

    services = compose["services"]
    for service_name in ("migrate", "app", "app-dev"):
        assert services[service_name]["environment"]["UMS_LOG_LEVEL"] == (
            _LOG_LEVEL_EXPRESSION
        )


def test_app_stop_grace_period_covers_connector_close_budgets() -> None:
    """Compose leaves enough time for executor and scheduler close paths."""
    compose = _load_compose()
    services = compose["services"]
    required_seconds = (
        scheduler._CLOSE_JOIN_TIMEOUT_SECONDS + executor.CLOSE_DRAIN_TIMEOUT_SECONDS
    )

    for service_name in ("app", "app-dev"):
        stop_grace_period = services[service_name]["stop_grace_period"]
        assert stop_grace_period == "120s"
        stop_grace_seconds = float(stop_grace_period.removesuffix("s"))
        assert stop_grace_seconds >= required_seconds
