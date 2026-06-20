"""CLI --connector dispatch registry.

B2.4 registers the YouTube Reporting keys. B2.5/B2.6 can add analytics and
AdSense keys without changing the CLI entrypoint. Unknown keys raise ValueError
at argparse time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ums_smart_revenue.connectors.google.errors import ConnectorAlreadyRegisteredError

if TYPE_CHECKING:
    # Imported only for type-checking to avoid a runtime import cycle:
    # orchestrator.py imports register_connector/dispatch_connector from this
    # module at load time, so this module cannot import orchestrator at runtime.
    from ums_smart_revenue.connectors.runs.orchestrator import ConnectorRunner

# The registry stores connector runner instances (e.g. YouTubeReportingRunner)
# that structurally satisfy the ConnectorRunner Protocol. Each runner carries
# per-connector configuration and exposes produce_reports(); typing the value
# as the Protocol (rather than Callable[..., Any]) lets mypy verify that every
# registered runner honors the dispatch contract the orchestrator relies on.
# Declared with the PEP 695 ``type`` statement so the alias resolves lazily:
# ConnectorRunner is only imported under TYPE_CHECKING, so a plain runtime
# assignment (``_RunnerFn = ConnectorRunner``) would raise NameError at import.
type _RunnerFn = ConnectorRunner
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
    """Register a connector runner under the given key.

    Raises:
        ConnectorAlreadyRegisteredError: If a connector is already registered with the given key.
    """
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
    """Resolve and return the connector runner associated with the key.

    Raises:
        ValueError: If the connector key is not registered.
    """
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
    """Return a sorted tuple of all registered connector keys.

    This is used for CLI validation and help text.
    """
    return tuple(sorted(_REGISTRY))
