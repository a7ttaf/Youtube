# ============================================================================
# Purpose: Pin the container operational contract for logging verbosity,
#   graceful executor drain, and dependency readiness.
# Database/ORM: None; this test inspects deployment manifests only.
# Standards: Read-only manifest assertions; no Docker daemon or credentials are
#   required for the unit test.
# Blast Radius: Test-only.
# Connections:
#   - File: docker-compose.yml -> app/app-dev environment and stop grace.
#   - File: Dockerfile -> image healthcheck endpoint.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> drain cap.
# ============================================================================
"""Regression tests for the Compose/Docker operational contract."""

from pathlib import Path

_ROOT = Path(__file__).parents[2]


def test_compose_forwards_log_level_and_gracefully_drains_workers():
    """Both app variants inherit the configured log level and 120s grace."""
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "UMS_LOG_LEVEL: ${UMS_LOG_LEVEL:-INFO}" in compose
    assert compose.count("stop_grace_period: 120s") == 2
    assert "http://localhost:8000/readyz" in compose


def test_image_healthcheck_uses_dependency_readiness_not_liveness():
    """The image probe must fail when the database dependency is unavailable."""
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "http://localhost:8000/readyz" in dockerfile
    assert "http://localhost:8000/livez" not in dockerfile
