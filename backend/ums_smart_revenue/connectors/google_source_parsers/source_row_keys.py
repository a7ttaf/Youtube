"""Deterministic source_row_key derivation per source_system.

Returns the full 64-char SHA-256 hex digest of a canonical string built
from the inputs. The canonical string is source-system-specific so two
different source systems can never collide even on identical
identifiers.
"""

import hashlib
import json
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
# Standards: Pure function. The canonical input is a structured JSON document
#            (json.dumps with sort_keys + tight separators), NOT a
#            delimiter-joined string. JSON quoting/escaping makes field and
#            dimension boundaries unambiguous, so two distinct rows can never
#            serialise to the same canonical form (no |/&/= collision). The
#            source-system prefix keeps identical identifiers in different
#            systems distinct.
# Blast Radius: Idempotency of source-row ingestion depends on this hash
#               being stable across runs. google_revenue_source_rows is new
#               in this PR with no persisted keys, so changing the
#               canonical form has no production-data impact. No graph
#               projection impact detected.
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
        canonical_payload: dict[str, object] = {
            "prefix": prefix,
            "source_report_id": fields["source_report_id"],
            "line_index": fields["line_index"],
            "dimensions": _canonical_dimensions(fields.get("dimensions") or {}),
        }
    elif source_system == "youtube_analytics":
        canonical_payload = {
            "prefix": prefix,
            "query_signature": fields["query_signature"],
            # currency + filters are distinct dataset axes: the same
            # ids/metrics/dimensions/period fetched in a different currency or
            # with a different filter expression is a different source row.
            "currency": fields.get("currency"),
            "filters": fields.get("filters"),
            "period_start": fields["period_start"],
            "period_end": fields["period_end"],
            "dimensions": _canonical_dimensions(fields.get("dimensions") or {}),
        }
    else:  # adsense_management
        canonical_payload = {
            "prefix": prefix,
            "source_report_id": fields["source_report_id"],
            "account_id": fields["account_id"],
            "period_start": fields["period_start"],
            "period_end": fields["period_end"],
            "dimensions": _canonical_dimensions(fields.get("dimensions") or {}),
        }

    # sort_keys gives cross-process stability; tight separators keep the digest
    # input compact. JSON escaping is what removes the delimiter-collision risk.
    canonical = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_dimensions(dimensions: dict[str, object]) -> list[list[object]]:
    """Stable, key-sorted [key, value] pairs for a dimensions dict.

    Returned as a JSON-serialisable list of [key, value] lists so the caller
    can embed it inside the canonical JSON payload. Sorting by key guarantees
    stability across runs regardless of dict insertion order. Because the
    surrounding json.dumps escapes every key and value, a dimension value
    containing '&', '=', or '|' can no longer collide with a different
    dimension set (the previous "&".join(f"{k}={v}") form could).
    """
    return [[key, value] for key, value in sorted(dimensions.items(), key=lambda kv: kv[0])]
