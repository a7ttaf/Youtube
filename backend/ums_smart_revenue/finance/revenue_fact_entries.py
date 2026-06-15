"""Revenue fact value objects shared by finance readers and calculators."""

from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api


# ============================================================================
# Purpose: Immutable revenue fact transfer object used by repositories,
#   reconciliation, allocation, payment matching, and API serializers.
# Database/ORM: None directly; populated from MonthlyChannelRevenueFactORM by
#   repository/readiness adapters.
# Standards: Read-only serialization helper with preserved decimal formatting.
# Blast Radius: Finance API/export shape only; PostgreSQL remains the source of
#   truth and no Neo4j projection behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> Re-exports and
#     constructs this entry from source-of-truth rows.
#   - File: backend/ums_smart_revenue/finance/reconciliation.py -> Consumes the
#     value object without importing the repository module.
# ============================================================================
@dataclass(frozen=True)
class RevenueFactEntry:
    """Revenue fact entry with identifiers, metrics, and metadata."""

    id: str
    month: str
    youtube_channel_id: str
    source_kind: str
    source_report_id: str | None
    gross_revenue_usd: Decimal
    net_revenue_usd: Decimal | None
    views: int
    watch_time_minutes: Decimal
    confidence_score: Decimal
    imported_by: str | None
    shorts_revenue_usd: Decimal | None = None
    longform_revenue_usd: Decimal | None = None
    subscription_revenue_usd: Decimal | None = None

    @property
    def audit_entity_id(self) -> str:
        return f"{self.youtube_channel_id}:{self.month}:{self.source_kind}"

    def to_api(self) -> dict[str, object]:
        """Convert this revenue fact instance into a dictionary suitable for API
        responses.
        """
        return {
            "id": self.id,
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "source_kind": self.source_kind,
            "source_report_id": self.source_report_id,
            "gross_revenue_usd": _decimal_to_api(self.gross_revenue_usd),
            "net_revenue_usd": _decimal_to_api(self.net_revenue_usd),
            "shorts_revenue_usd": _decimal_to_api(self.shorts_revenue_usd),
            "longform_revenue_usd": _decimal_to_api(self.longform_revenue_usd),
            "subscription_revenue_usd": _decimal_to_api(self.subscription_revenue_usd),
            "views": self.views,
            "watch_time_minutes": _decimal_to_api(self.watch_time_minutes),
            "confidence_score": _decimal_to_api(self.confidence_score),
            "imported_by": self.imported_by,
        }
