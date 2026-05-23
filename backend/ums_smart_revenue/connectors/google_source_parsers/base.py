"""Parser protocol shared by every Google source-system parser.

Parsers receive a pre-recorded payload + a tenant_id and emit
ParsedSourceRow instances. They are the only place where
source_row_key is derived (via source_row_keys.build_source_row_key).
Repositories never re-derive the key.
"""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation
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
    """Raise ParserError if d[key] is missing or not an int.

    bool is a subclass of int, so an explicit bool guard is required: without
    it ``True``/``False`` would be accepted and silently normalised to 1/0,
    which can collide with legitimate integer rows (e.g. line_index) and
    corrupt source_row_key dedup.
    """
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParserError(f"missing or non-int field: {key!r}")
    return value


def parse_decimal_amount(raw_value: str, *, metric_key: str) -> Decimal:
    """Parse a string-typed money value into a Decimal, rejecting NaN/Infinity.

    Decimal("NaN") and Decimal("Infinity") construct cleanly, so without an
    explicit check they would flow downstream and the repository's
    `amount_native >= 0` guard would surface NaN as a cryptic
    InvalidOperation. Fail with a labeled ParserError at the parser
    boundary instead.
    """
    try:
        amount = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ParserError(
            f"metric {metric_key!r} value must be a valid Decimal string, got {raw_value!r}"
        ) from exc
    if not amount.is_finite():
        raise ParserError(
            f"metric {metric_key!r} value must be finite, got {raw_value!r}"
        )
    return amount


def parse_iso_date(raw_value: str, *, field: str) -> date:
    """Parse an ISO-8601 date string, raising ParserError on malformed input.

    date.fromisoformat raises a bare ValueError for malformed values, which
    would escape the parser's typed failure contract. Wrapping it here keeps
    every parser's date handling on the ParserError path so callers can
    translate parser failures uniformly.
    """
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise ParserError(
            f"{field} must be an ISO-8601 date string, got {raw_value!r}"
        ) from exc


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
