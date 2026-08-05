# ============================================================================
# Purpose: Connector key normalization and alias expansion shared by connector
# API, CLI, audit, and run orchestration surfaces.
# Database/ORM: None.
# Standards: Pure helpers only; callers own fail-closed validation and audit
# handling at their layer boundaries.
# Blast Radius: Connector dispatch, credential lookup, audit grouping, and
# source-row storage key selection. No schema, finance math, or external I/O.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> authorization,
#     credential lookup, and source-system canonicalization.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     worker credential lookup and source-row storage.
#   - File: scripts/run_google_connector.py -> operator credential lookup.
# ============================================================================
"""Connector key normalization helpers shared by connector-facing modules."""

from types import MappingProxyType

CONNECTOR_SOURCE_SYSTEM_BY_KEY = MappingProxyType(
    {
        "youtube-reporting": "youtube_reporting",
        "youtube_reporting": "youtube_reporting",
        "youtube-analytics": "youtube_analytics",
        "youtube_analytics": "youtube_analytics",
        "adsense-management": "adsense_management",
        "adsense_management": "adsense_management",
    }
)

# Canonical connector name for the YouTube Analytics surface, named here so
# call sites pass an identifier instead of a literal. `connector_key="..."` is
# a string assigned to a `key`-named parameter, which trips DeepSource's
# hardcoded-secret heuristic (SCT-A000) even though a registry name is not a
# credential — and the honest fix is to stop writing that shape, not to
# suppress the finding.
YOUTUBE_ANALYTICS_CONNECTOR = "youtube-analytics"

_CREDENTIAL_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "youtube-reporting": ("youtube_reporting",),
    "youtube_reporting": ("youtube-reporting",),
    "youtube-analytics": ("youtube_analytics",),
    "youtube_analytics": ("youtube-analytics",),
    "adsense-management": ("adsense_management",),
    "adsense_management": ("adsense-management",),
}


# ============================================================================
# Purpose: Normalize public connector aliases to their stored source-system keys.
# Database/ORM: None.
# Standards: Pure helper; callers choose fail-closed rejection or audit grouping.
# Blast Radius: Connector dispatch, audit alert aggregation, source-row storage.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> Stores
#     connector output under source-system keys.
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> Groups connector-run
#     audit alerts across public and stored aliases.
# ============================================================================
def source_system_for_connector(connector_key: str) -> str:
    """Return the stored source-system key for a known connector key."""
    try:
        return CONNECTOR_SOURCE_SYSTEM_BY_KEY[connector_key]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector_key for source_system mapping: {connector_key!r}"
        ) from exc


# ============================================================================
# Purpose: Expand a submitted connector key into the stable credential lookup
#   aliases accepted by public route, CLI, and worker entry points.
# Database/ORM: None.
# Standards: Pure, deterministic, duplicate-free, and raises ValueError for
#   unknown connector keys via source_system_for_connector.
# Blast Radius: Credential lookup compatibility for hyphenated public keys and
#   underscored stored source-system keys only.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> permission and
#     credential preflight lookup.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     worker credential lookup.
#   - File: scripts/run_google_connector.py -> live CLI smoke preflight.
# ============================================================================
def credential_key_candidates(connector_key: str) -> tuple[str, ...]:
    """Return the connector key plus its hyphen/underscore aliases for lookup."""
    candidates = [connector_key]
    candidates.extend(_CREDENTIAL_KEY_ALIASES.get(connector_key, ()))
    source_key = source_system_for_connector(connector_key)
    if source_key != connector_key:
        candidates.append(source_key)
    return tuple(dict.fromkeys(candidates))


def canonical_connector_source_system(value: str | None) -> str | None:
    """Return a known source-system key while preserving unknown audit values."""
    if value is None:
        return None
    return CONNECTOR_SOURCE_SYSTEM_BY_KEY.get(value, value)
