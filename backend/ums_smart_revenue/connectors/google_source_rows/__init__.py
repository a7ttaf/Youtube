"""Google revenue source-row storage repository and shared dataclasses.

Parsers (one level up at connectors/google_source_parsers/) emit
ParsedSourceRow instances; this package writes them to
google_revenue_source_rows.
"""

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ALLOWED_SOURCE_SYSTEMS,
    ALLOWED_VALUE_KINDS,
    SOURCE_ROW_KEY_LENGTH,
    CurrencyValidationError,
    GoogleRevenueSourceRowEntry,
    GoogleRevenueSourceRowError,
    GoogleRevenueSourceRowValidationError,
    IsoCurrency,
    ParsedSourceRow,
    SourceRowUpsertResult,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyCurrenciesRepository,
    SqlAlchemyGoogleRevenueSourceRowRepository,
)

__all__ = [
    "ALLOWED_SOURCE_SYSTEMS",
    "ALLOWED_VALUE_KINDS",
    "SOURCE_ROW_KEY_LENGTH",
    "CurrencyValidationError",
    "GoogleRevenueSourceRowEntry",
    "GoogleRevenueSourceRowError",
    "GoogleRevenueSourceRowValidationError",
    "IsoCurrency",
    "ParsedSourceRow",
    "SourceRowUpsertResult",
    "SqlAlchemyCurrenciesRepository",
    "SqlAlchemyGoogleRevenueSourceRowRepository",
]
