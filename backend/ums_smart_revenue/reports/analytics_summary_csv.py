# ============================================================================
# Purpose: Build deterministic analytics summary CSV artifacts from normalized
# YouTube Analytics source rows without exposing raw payload/account metadata.
# Database/ORM: google_revenue_source_rows and youtube_channels read-only queries.
# Standards: Service-layer CSV generation, USD-only export contract, injection
# guarded text cells, and typed validation errors.
# Blast Radius: Analytics exports, revenue amount disclosure, artifact checksums.
# Connections:
#   - File: backend/ums_smart_revenue/api/exports.py -> Download route/auth/audit.
#   - File: backend/ums_smart_revenue/db/source_models.py -> Source-row schema.
# ============================================================================
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import StringIO
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api
from ums_smart_revenue.reports.exports import ExportJobEntry


class AnalyticsSummaryCsvValidationError(ValueError):
    """Raised when an export job cannot produce an analytics summary CSV."""


@dataclass(frozen=True)
class AnalyticsSummaryCsvRow:
    report_month: str
    youtube_channel_id: str
    channel_name: str
    metric_key: str
    value_kind: str
    currency_code: str
    period_start: date | None
    period_end: date | None
    source_row_count: int
    amount_native: Decimal


_FIELDNAMES = (
    "report_month",
    "source_system",
    "youtube_channel_id",
    "channel_name",
    "metric_key",
    "value_kind",
    "currency_code",
    "period_start",
    "period_end",
    "source_row_count",
    "amount_native",
    "formula",
    "confidence",
)
_SOURCE_SYSTEM = "youtube_analytics"
_FORMULA = "SUM(google_revenue_source_rows.amount_native)"
_CONFIDENCE = "source_rows"
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_BLANK_CHANNEL_ID_CHARACTERS = (" ", "\t", "\r", "\n", "\f", "\v")


# ============================================================================
# Purpose: Build a scoped analytics CSV artifact from normalized Google source
# rows, preserving the aggregate source/formula/confidence contract while
# excluding raw payloads and account identifiers from the export surface.
# Database/ORM: google_revenue_source_rows and youtube_channels.
# Standards: Repository-like read logic stays out of the route; CSV output is
# injection-guarded and deterministic by channel/metric/value/currency.
# Blast Radius: Analytics exports, audit artifact checksums, source-row reads.
# Connections:
#   - File: backend/ums_smart_revenue/api/exports.py -> Download route and authz.
#   - File: backend/ums_smart_revenue/db/source_models.py -> Source-row contract.
# ============================================================================
def build_analytics_summary_csv(
    *,
    session: Session,
    tenant_id: UUID,
    export_job: ExportJobEntry,
    scope_channel_ids: set[str] | None,
) -> bytes:
    """Return UTF-8 CSV bytes for an ANALYTICS_SUMMARY_CSV export job."""
    if export_job.export_type != "ANALYTICS_SUMMARY_CSV":
        raise AnalyticsSummaryCsvValidationError(
            "analytics summary CSV download only supports ANALYTICS_SUMMARY_CSV exports"
        )
    export_currency = export_job.currency.upper()
    if export_currency != "USD":
        raise AnalyticsSummaryCsvValidationError(
            "analytics summary CSV exports currently support USD only"
        )
    rows = list_analytics_summary_csv_rows(
        session=session,
        tenant_id=tenant_id,
        month=export_job.month,
        currency_code=export_currency,
        scope_channel_ids=scope_channel_ids,
    )
    return render_analytics_summary_csv(report_month=export_job.month, rows=rows)


def list_analytics_summary_csv_rows(
    *,
    session: Session,
    tenant_id: UUID,
    month: str,
    currency_code: str,
    scope_channel_ids: set[str] | None,
) -> tuple[AnalyticsSummaryCsvRow, ...]:
    """Aggregate YouTube Analytics source rows for one tenant/month/scope."""
    if scope_channel_ids is not None and not scope_channel_ids:
        return ()

    source_row = GoogleRevenueSourceRowORM
    channel = YouTubeChannelORM
    channel_id_without_blank_chars = source_row.youtube_channel_id
    for blank_character in _BLANK_CHANNEL_ID_CHARACTERS:
        channel_id_without_blank_chars = sa.func.replace(
            channel_id_without_blank_chars,
            blank_character,
            "",
        )
    stmt = (
        sa.select(
            source_row.youtube_channel_id,
            channel.channel_name,
            source_row.metric_key,
            source_row.value_kind,
            source_row.currency_code,
            sa.func.min(source_row.period_start),
            sa.func.max(source_row.period_end),
            sa.func.count(source_row.id),
            sa.func.coalesce(sa.func.sum(source_row.amount_native), 0),
        )
        .select_from(source_row)
        .join(
            channel,
            sa.and_(
                channel.tenant_id == source_row.tenant_id,
                channel.youtube_channel_id == source_row.youtube_channel_id,
            ),
            isouter=True,
        )
        .where(
            source_row.tenant_id == tenant_id,
            source_row.source_system == _SOURCE_SYSTEM,
            source_row.report_month == month,
            source_row.currency_code == currency_code,
            source_row.youtube_channel_id.is_not(None),
            # FIX: SQL trim() only strips spaces by default on PostgreSQL/SQLite;
            # remove ASCII whitespace so tab/newline-only IDs never enter CSV rows.
            sa.func.length(channel_id_without_blank_chars) > 0,
        )
        .group_by(
            source_row.youtube_channel_id,
            channel.channel_name,
            source_row.metric_key,
            source_row.value_kind,
            source_row.currency_code,
        )
        .order_by(
            source_row.youtube_channel_id.asc(),
            source_row.metric_key.asc(),
            source_row.value_kind.asc(),
            source_row.currency_code.asc(),
        )
    )
    if scope_channel_ids is not None:
        stmt = stmt.where(source_row.youtube_channel_id.in_(sorted(scope_channel_ids)))

    rows: list[AnalyticsSummaryCsvRow] = []
    for (
        youtube_channel_id,
        channel_name,
        metric_key,
        value_kind,
        row_currency_code,
        period_start,
        period_end,
        source_row_count,
        amount_native,
    ) in session.execute(stmt):
        rows.append(
            AnalyticsSummaryCsvRow(
                report_month=month,
                youtube_channel_id=youtube_channel_id,
                channel_name=channel_name or "",
                metric_key=metric_key,
                value_kind=value_kind,
                currency_code=row_currency_code,
                period_start=period_start,
                period_end=period_end,
                source_row_count=source_row_count,
                amount_native=amount_native,
            )
        )
    return tuple(rows)


def render_analytics_summary_csv(
    *,
    report_month: str,
    rows: tuple[AnalyticsSummaryCsvRow, ...],
) -> bytes:
    """Render analytics summary rows as injection-safe UTF-8 CSV bytes."""
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "report_month": _csv_safe(report_month),
                "source_system": _csv_safe(_SOURCE_SYSTEM),
                "youtube_channel_id": _csv_safe(row.youtube_channel_id),
                "channel_name": _csv_safe(row.channel_name),
                "metric_key": _csv_safe(row.metric_key),
                "value_kind": _csv_safe(row.value_kind),
                "currency_code": _csv_safe(row.currency_code),
                "period_start": _csv_safe(row.period_start.isoformat() if row.period_start else ""),
                "period_end": _csv_safe(row.period_end.isoformat() if row.period_end else ""),
                "source_row_count": str(row.source_row_count),
                "amount_native": decimal_to_api(row.amount_native),
                "formula": _csv_safe(_FORMULA),
                "confidence": _csv_safe(_CONFIDENCE),
            }
        )
    return buffer.getvalue().encode("utf-8")


def _csv_safe(value: object) -> str:
    """Prefix formula-like CSV cells so spreadsheet tools treat them as text."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_INJECTION_PREFIXES):
        return f"'{text}"
    return text
