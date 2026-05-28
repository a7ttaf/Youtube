"""CLI --connector dispatch registry.

B2.4 registers the YouTube Reporting keys. B2.5/B2.6 can add analytics and
AdSense keys without changing the CLI entrypoint. Unknown keys raise ValueError
at argparse time.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ums_smart_revenue.connectors.google.errors import ConnectorAlreadyRegisteredError

_RunnerFn = Callable[..., Any]
_REGISTRY: dict[str, _RunnerFn] = {}

# Canonical operator-facing key for the AdSense Management connector slice.
# Shared by the live payment-sync service and the run orchestrator so the
# credential row is resolved under a single source-of-truth string.
ADSENSE_MANAGEMENT_CONNECTOR_KEY = "adsense-management"


# ============================================================================
# Purpose: Register a connector runner behind its operator-facing key.
# Database/ORM: None.
# Standards: Fail-fast duplicate keys; route/CLI code only dispatches through
#            this registry and never imports concrete runners directly.
# Blast Radius: Operator dispatch only. Finance rows, authorization, audit,
#               Neo4j, and exports are unaffected until the runner executes.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     Module-load runner registration.
#   - File: scripts/run_google_connector.py ->
#     argparse choices from known_keys().
# ============================================================================
def register_connector(*, key: str, runner: _RunnerFn) -> None:
    if key in _REGISTRY:
        raise ConnectorAlreadyRegisteredError(key=key)
    _REGISTRY[key] = runner


# ============================================================================
# Purpose: Resolve a connector key to its registered runner.
# Database/ORM: None.
# Standards: Unknown keys stay ValueError for argparse compatibility.
# Blast Radius: Operator dispatch only. Downstream DB/finance writes happen in
#               the selected runner/orchestrator path, not here.
# Connections:
#   - File: scripts/run_google_connector.py ->
#     Calls this indirectly through run_one().
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     Dispatches live and dry-run paths.
# ============================================================================
def dispatch_connector(*, key: str) -> _RunnerFn:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"unknown connector key: {key!r}") from exc


# ============================================================================
# Purpose: Return the currently registered connector keys for CLI validation.
# Database/ORM: None.
# Standards: Stable sorted tuple; callers cannot mutate registry state.
# Blast Radius: Operator help/argparse only.
# Connections:
#   - File: scripts/run_google_connector.py -> argparse choices.
# ============================================================================
def known_keys() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
