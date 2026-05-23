"""Deterministic source_row_key derivation per source_system.

Returns the full 64-char SHA-256 hex digest of a canonical string built
from the inputs. The canonical string is source-system-specific so two
different source systems can never collide even on identical
identifiers.
"""

import hashlib
from typing import Final

_PREFIX: Final[dict[str, str]] = {
    "youtube_reporting": "yt-rep",
    "youtube_analytics": "yt-ana",
    "adsense_management": "adsense",
}


# ============================================================================
# Purpose: Derive the deterministic 64-char SHA-256 source_row_key that the
#          storage repository keys on (tenant_id, source_system,
#          source_row_key). Parsers are the only producers of this value;
#          repositories never re-derive it.
# Database/ORM: None directly. The output is written to
#               google_revenue_source_rows.source_row_key by the repository.
# Standards: Pure function. Source-system-specific canonical string before
#            hashing so identical identifiers in two different systems can
#            never collide.
# Blast Radius: Idempotency of source-row ingestion depends on this hash
#               being stable across runs. No graph projection impact
#               detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_rows/dataclasses.py
#     -> ParsedSourceRow.source_row_key consumer.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/base.py
#     -> Parser protocol that calls this helper.
# ============================================================================
def build_source_row_key(*, source_system: str, **fields: object) -> str:
    if source_system not in _PREFIX:
        raise ValueError(f"unknown source_system: {source_system!r}")
    prefix = _PREFIX[source_system]

    if source_system == "youtube_reporting":
        canonical = (
            f"{prefix}|"
            f"{fields['source_report_id']}|"
            f"{fields['line_index']}|"
            f"{_canonical_dimensions(fields.get('dimensions') or {})}"
        )
    elif source_system == "youtube_analytics":
        canonical = (
            f"{prefix}|"
            f"{fields['query_signature']}|"
            f"{fields['period_start']}|"
            f"{fields['period_end']}|"
            f"{_canonical_dimensions(fields.get('dimensions') or {})}"
        )
    else:  # adsense_management
        canonical = (
            f"{prefix}|"
            f"{fields['source_report_id']}|"
            f"{fields['account_id']}|"
            f"{fields['period_start']}|"
            f"{fields['period_end']}|"
            f"{_canonical_dimensions(fields.get('dimensions') or {})}"
        )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_dimensions(dimensions: dict[str, object]) -> str:
    """Stable tuple representation of a dimensions dict.

    Dict iteration order is insertion-stable in CPython but this is a
    cross-process key; we sort by key to guarantee stability across runs.
    """
    sorted_items = sorted(dimensions.items(), key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in sorted_items)
