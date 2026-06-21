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


def canonical_connector_source_system(value: str | None) -> str | None:
    """Return a known source-system key while preserving unknown audit values."""
    if value is None:
        return None
    return CONNECTOR_SOURCE_SYSTEM_BY_KEY.get(value, value)
