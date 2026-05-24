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
        # Keyed on the STABLE logical identity: report_type + currency + period
        # + dimensions. Both source_report_id and line_index are intentionally
        # excluded — YouTube Reporting emits replacement/backfilled reports for
        # the same window with a NEW report id (newer createTime) and unsorted
        # rows, so keying on the report id or row position would make corrected
        # reruns insert duplicate rows instead of updating the prior rows for
        # the same period/dimensions.
        # FIX: currency is part of the row's logical identity and is now a
        # REQUIRED key axis (bracket access), mirroring the youtube_analytics
        # and adsense_management branches below. The parser stamps a per-row
        # metrics.currencyCode, so the same report_type/period/dimensions
        # reported in a different currency is a distinct monetary row; omitting
        # currency let two distinct-currency rows collide onto one upsert key
        # and silently overwrite each other (CLAUDE.md rule 4).
        canonical_payload: dict[str, object] = {
            "prefix": prefix,
            "report_type": fields["report_type"],
            "currency": fields["currency"],
            "period_start": fields["period_start"],
            "period_end": fields["period_end"],
            "dimensions": _canonical_dimensions(fields.get("dimensions") or {}),
        }
    elif source_system == "youtube_analytics":
        canonical_payload = {
            "prefix": prefix,
            "query_signature": fields["query_signature"],
            # currency + filters are distinct dataset axes: the same
            # ids/metrics/dimensions/period fetched in a different currency or
            # with a different filter expression is a different source row.
            # FIX: currency is REQUIRED (bracket access): a caller that omits it
            # now fails closed with KeyError instead of silently hashing None and
            # collapsing distinct-currency rows onto one upsert key (CLAUDE.md
            # rule 4). filters stays optional (.get) — a query may carry none.
            "currency": fields["currency"],
            "filters": fields.get("filters"),
            "period_start": fields["period_start"],
            "period_end": fields["period_end"],
            "dimensions": _canonical_dimensions(fields.get("dimensions") or {}),
        }
    else:  # adsense_management
        # Keyed on the STABLE logical identity: metric + account + period +
        # dimensions. The run-specific report_id is intentionally excluded (and
        # is preserved separately as source_report_id provenance): AdSense
        # regenerates reports for the same month/account/dimensions under a new
        # report identifier, so folding report_id into the key would make a
        # rerun insert duplicate financial rows instead of updating the prior
        # ones — the same idempotency hazard fixed for youtube_reporting.
        canonical_payload = {
            "prefix": prefix,
            "metric_key": fields["metric_key"],
            # currency is part of the row's logical identity: the same
            # metric/account/period/dimensions reported in a different currency
            # is a distinct monetary row, so it must not collapse to one upsert
            # key (mirrors the youtube_analytics branch above).
            # FIX: required (bracket access) — omitting currency now fails closed
            # with KeyError rather than silently hashing None (CodeRabbit).
            "currency": fields["currency"],
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
