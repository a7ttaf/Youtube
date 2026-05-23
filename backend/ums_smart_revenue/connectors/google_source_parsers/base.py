"""Parser protocol shared by every Google source-system parser.

Parsers receive a pre-recorded payload + a tenant_id and emit
ParsedSourceRow instances. They are the only place where
source_row_key is derived (via source_row_keys.build_source_row_key).
Repositories never re-derive the key.
"""

from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow


class ParserError(ValueError):
    """Raised when a payload is malformed or violates the parser's contract."""


# ============================================================================
# Purpose: Shared payload-shape guards used by every Google source parser.
#          Centralising them keeps error message wording consistent across
#          parsers and removes per-parser duplication.
# Database/ORM: None.
# Standards: Pure functions. Raise ParserError on any shape violation so the
#            caller can translate to a typed boundary error.
# Blast Radius: No DB write. Used inside parsers only. No graph projection
#               impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_reporting.py, youtube_analytics.py, adsense_management.py
#     -> Replace per-parser staticmethod helpers with these imports.
# ============================================================================
def require_dict(d: dict[str, object], key: str) -> dict[str, object]:
    """Raise ParserError if d[key] is missing or not a dict."""
    value = d.get(key)
    if not isinstance(value, dict):
        raise ParserError(f"missing or non-dict field: {key!r}")
    return value


def require_str(d: dict[str, object], key: str) -> str:
    """Raise ParserError if d[key] is missing or not a string."""
    value = d.get(key)
    if not isinstance(value, str):
        raise ParserError(f"missing or non-str field: {key!r}")
    return value


def require_int(d: dict[str, object], key: str) -> int:
    """Raise ParserError if d[key] is missing or not an int."""
    value = d.get(key)
    if not isinstance(value, int):
        raise ParserError(f"missing or non-int field: {key!r}")
    return value


class SourceRowParser(Protocol):
    source_system: str

    def parse(
        self,
        payload: dict[str, object],
        *,
        tenant_id: UUID,
    ) -> Iterable[ParsedSourceRow]:
        """Translate a single pre-recorded report payload into ParsedSourceRow rows."""
        ...
