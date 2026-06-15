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
    """Raise ParserError if d is not a mapping, or d[key] is missing/not a dict.

    The container guard matters at the parse() entry point: each parser's first
    call is require_dict(payload, ...), so a non-object top-level payload (a
    JSON list/string/number) would otherwise raise a raw AttributeError on
    ``.get`` instead of the parser's typed ParserError, bypassing callers that
    handle parser failures uniformly.
    """
    if not isinstance(d, dict):
        raise ParserError(f"expected an object to read field {key!r} from")
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


def parse_decimal_amount(raw_value: object, *, metric_key: str) -> Decimal:
    """Normalise a source money value into a finite Decimal.

    Accepts the JSON shapes Google revenue payloads actually emit: Decimal
    strings (AdSense report cells, YouTube Reporting CSV) and JSON numbers
    (YouTube Analytics reports.query FLOAT/INTEGER cells, which json.loads
    yields as float/int). Floats are routed through str() so the shortest
    round-trip decimal is used, avoiding binary artifacts such as
    Decimal(0.1) == Decimal("0.1000000000000000055..."). bool is rejected
    explicitly because it is an int subclass yet is never a real money value.
    NaN and Infinity construct cleanly as Decimal, so they are rejected here
    rather than surfacing later as a cryptic InvalidOperation at the
    repository's `amount_native >= 0` guard.
    """
    if isinstance(raw_value, bool):
        raise ParserError(
            f"metric {metric_key!r} value must be a number or Decimal string, got bool"
        )
    # FIX: accept JSON numeric metric cells (int/float from reports.query, kept
    # distinct from the bool case rejected above) and normalize via str() into a
    # Decimal; the prior str-only contract rejected valid YouTube Analytics
    # payloads. raw_value -> candidate; non-numeric/non-str still raises ParserError.
    if isinstance(raw_value, (int, float)):
        candidate = Decimal(str(raw_value))
    elif isinstance(raw_value, str):
        try:
            candidate = Decimal(raw_value)
        except InvalidOperation as exc:
            raise ParserError(
                f"metric {metric_key!r} value must be a valid Decimal string, got {raw_value!r}"
            ) from exc
    else:
        raise ParserError(
            f"metric {metric_key!r} value must be a number or Decimal string, "
            f"got {type(raw_value).__name__}"
        )
    if not candidate.is_finite():
        raise ParserError(f"metric {metric_key!r} value must be finite, got {raw_value!r}")
    return candidate


def parse_iso_date(raw_value: str, *, field: str) -> date:
    """Parse a strict YYYY-MM-DD date string, raising ParserError otherwise.

    date.fromisoformat raises a bare ValueError for malformed values, which
    would escape the parser's typed failure contract. It also (Python 3.11+)
    accepts non-canonical ISO forms the YouTube APIs never emit — compact
    YYYYMMDD and week dates like 2026-W14-1 — which would be silently
    normalised to an unintended calendar day. Wrapping it here keeps every
    parser's date handling on the ParserError path, and the isoformat()
    round-trip enforces the exact YYYY-MM-DD contract: the zero-padded
    canonical form is the only string date.fromisoformat both accepts and
    reproduces, so anything else fails closed.
    """
    try:
        parsed = date.fromisoformat(raw_value)
    except ValueError as exc:
        raise ParserError(f"{field} must be an ISO-8601 date string, got {raw_value!r}") from exc
    if parsed.isoformat() != raw_value:
        raise ParserError(f"{field} must be a canonical YYYY-MM-DD date string, got {raw_value!r}")
    return parsed


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
