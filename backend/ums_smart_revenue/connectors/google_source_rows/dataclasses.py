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
    """Represents an ISO currency with code, numeric code, name, minor unit,
    support flag, and activation time.
    """

    code: str
    numeric_code: str
    name: str
    minor_unit: int | None
    is_supported: bool
    activated_at: datetime | None


@dataclass(frozen=True)
class ParsedSourceRow:
    """Immutable parsed source row boundary type containing metadata fields
    and raw payload from the data source.
    """

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
    """Immutable representation of a Google revenue source row entry
    with metadata, amounts, and provenance fields.
    """

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


@dataclass(frozen=True)
class SourceRowUpsertResult:
    """Persisted source rows plus the created/updated/unchanged split."""

    # ============================================================================
    # Purpose: Return shape of SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many
    #          carrying the persisted entries alongside the per-row classification
    #          counts the orchestrator copies into connector_runs.counts_json.
    # Database/ORM: None directly; populated from a pre-fetch of existing
    #               google_revenue_source_rows compared to the input ParsedSourceRow
    #               batch.
    # Standards: Sum invariant — len(entries) == created + updated + unchanged.
    #            "Unchanged" means every parser-owned content field matches the
    #            existing row; raw_file_id / imported_by are provenance, not
    #            content, and their refresh is not counted as a value update.
    # Blast Radius: connector_runs.counts_json accuracy; finance source rows
    #               themselves are unaffected by this dataclass.
    # ============================================================================
    entries: list[GoogleRevenueSourceRowEntry]
    created: int
    updated: int
    unchanged: int


class GoogleRevenueSourceRowError(ValueError):
    """Base class for source-row repository errors."""


class GoogleRevenueSourceRowValidationError(GoogleRevenueSourceRowError):
    """Raised when a ParsedSourceRow fails validation before write."""


class CurrencyValidationError(ValueError):
    """Raised by currency lookup helpers when a code is unknown or malformed."""
