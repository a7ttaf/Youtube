"""Immutable IO dataclasses + error contract for the source-row repository.

ParsedSourceRow is the parser/repository boundary type. The 64-char
source_row_key is computed by parsers (see
connectors/google_source_parsers/source_row_keys.py) and never
recomputed inside the repository.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

ALLOWED_SOURCE_SYSTEMS: Final[frozenset[str]] = frozenset(
    {"youtube_reporting", "youtube_analytics", "adsense_management"}
)
ALLOWED_VALUE_KINDS: Final[frozenset[str]] = frozenset(
    {"estimated", "settled", "adjustment", "tax", "deduction"}
)
SOURCE_ROW_KEY_LENGTH: Final[int] = 64  # SHA-256 hex digest length


@dataclass(frozen=True)
class IsoCurrency:
    code: str
    numeric_code: str
    name: str
    minor_unit: int | None
    is_supported: bool
    activated_at: datetime | None


@dataclass(frozen=True)
class ParsedSourceRow:
    source_system: str
    source_row_key: str
    source_account_id: str
    content_owner_id: str | None
    youtube_channel_id: str | None
    report_type: str
    report_month: str  # YYYY-MM
    period_start: date
    period_end: date
    metric_key: str
    value_kind: str
    amount_native: Decimal
    currency_code: str
    source_report_id: str | None
    raw_payload: dict[str, object]


@dataclass(frozen=True)
class GoogleRevenueSourceRowEntry:
    id: str
    tenant_id: str
    source_system: str
    source_row_key: str
    source_account_id: str
    content_owner_id: str | None
    youtube_channel_id: str | None
    report_type: str
    report_month: str
    period_start: date
    period_end: date
    metric_key: str
    value_kind: str
    amount_native: Decimal
    currency_code: str
    source_report_id: str | None
    raw_file_id: str | None
    raw_payload: dict[str, object]
    imported_by: str | None
    ingested_at: datetime


class GoogleRevenueSourceRowError(ValueError):
    """Base class for source-row repository errors."""


class GoogleRevenueSourceRowValidationError(GoogleRevenueSourceRowError):
    """Raised when a ParsedSourceRow fails validation before write."""


class CurrencyValidationError(ValueError):
    """Raised by currency lookup helpers when a code is unknown or malformed."""
