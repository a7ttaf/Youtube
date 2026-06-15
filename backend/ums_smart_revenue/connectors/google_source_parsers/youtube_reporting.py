"""Parser for YouTube Reporting API report payloads.

Consumes a pre-recorded payload shaped like the parser-friendly JSON
the connector would emit after converting the upstream CSV report into
a dict. Emits ParsedSourceRow instances with value_kind='estimated'.
"""

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from ums_smart_revenue.connectors.google_source_parsers.base import (
    ParserError,
    parse_decimal_amount,
    parse_iso_date,
    require_dict,
    require_int,
    require_str,
)
from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
    build_source_row_key,
)
from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow


@dataclass(frozen=True)
class _ReportingRowContext:
    raw_row: dict[str, object]
    period_start: date
    period_end: date
    channel: str
    content_owner: str | None
    amount_raw: str
    currency: str
    key_dimensions: dict[str, object]


# ============================================================================
# Purpose: Validate YouTube Reporting parser rows before source-row emission.
# Database/ORM: None. ParsedSourceRow boundary.
# Standards: Typed ParserError failures, stable source-row identity axes.
# Blast Radius: Finance ingestion provenance; no graph projection impact detected.
# Connections:
#   - File: tests/connectors/google_source_parsers/test_youtube_reporting_parser.py
#     -> Parser contract.
#   - File: backend/ums_smart_revenue/connectors/google_source_rows.py -> ParsedSourceRow shape.
# ============================================================================
def _reporting_rows(payload: dict[str, object]) -> list[object]:
    """Return Reporting rows after validating the top-level row container."""
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ParserError("payload['rows'] must be a list")
    return rows


def _reporting_row_context(row: object) -> _ReportingRowContext:
    """Validate one YouTube Reporting row and normalize identity fields."""
    if not isinstance(row, dict):
        raise ParserError("each rows[*] must be a dict")
    require_int(row, "line_index")
    period_start, period_end = _reporting_period(row)
    dimensions = require_dict(row, "dimensions")
    metrics = require_dict(row, "metrics")
    channel, content_owner, key_dimensions = _reporting_identity(dimensions)
    amount_raw = metrics.get("estimatedRevenue")
    if not isinstance(amount_raw, str):
        raise ParserError("metrics.estimatedRevenue must be a string for Decimal precision")
    currency = _reporting_currency(metrics)
    return _ReportingRowContext(
        raw_row=row,
        period_start=period_start,
        period_end=period_end,
        channel=channel,
        content_owner=content_owner,
        amount_raw=amount_raw,
        currency=currency,
        key_dimensions=key_dimensions,
    )


def _reporting_period(row: dict[str, object]) -> tuple[date, date]:
    """Parse and validate one Reporting row's single-month date range."""
    date_range = require_dict(row, "date_range")
    period_start = parse_iso_date(require_str(date_range, "start"), field="date_range.start")
    period_end = parse_iso_date(require_str(date_range, "end"), field="date_range.end")
    if period_end < period_start:
        raise ParserError(
            "row date_range.end must be on or after start, got "
            f"{period_start.isoformat()}..{period_end.isoformat()}"
        )
    if (period_start.year, period_start.month) != (period_end.year, period_end.month):
        raise ParserError(
            "row date_range must fall within a single calendar month for "
            f"report_month bucketing, got {period_start.isoformat()}..{period_end.isoformat()}"
        )
    return period_start, period_end


def _reporting_identity(
    dimensions: dict[str, object],
) -> tuple[str, str | None, dict[str, object]]:
    """Normalize required channel and optional content-owner identity dimensions."""
    channel = dimensions.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise ParserError("dimensions.channel must be a non-empty string")
    channel = channel.strip()

    content_owner = dimensions.get("content_owner")
    if content_owner is not None and not isinstance(content_owner, str):
        raise ParserError("dimensions.content_owner must be a string when present")
    content_owner = content_owner.strip() if isinstance(content_owner, str) else None
    if not content_owner:
        content_owner = None

    key_dimensions = {**dimensions, "channel": channel}
    if content_owner is None:
        key_dimensions.pop("content_owner", None)
    else:
        key_dimensions["content_owner"] = content_owner
    return channel, content_owner, key_dimensions


def _reporting_currency(metrics: dict[str, object]) -> str:
    """Trim and validate the source currency for one Reporting metric row."""
    currency = metrics.get("currencyCode")
    if not isinstance(currency, str) or not currency.strip():
        raise ParserError("metrics.currencyCode must be a non-empty string")
    return currency.strip()


def _reporting_source_row(
    *,
    source_system: str,
    report_id: str,
    report_type: str,
    context: _ReportingRowContext,
) -> ParsedSourceRow:
    """Build the immutable source row from one normalized Reporting row."""
    source_row_key = build_source_row_key(
        source_system=source_system,
        report_type=report_type,
        currency=context.currency,
        period_start=context.period_start.isoformat(),
        period_end=context.period_end.isoformat(),
        dimensions=context.key_dimensions,
    )
    return ParsedSourceRow(
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id=(
            context.content_owner if context.content_owner is not None else context.channel
        ),
        content_owner_id=context.content_owner,
        youtube_channel_id=context.channel,
        report_type=report_type,
        report_month=f"{context.period_start.year:04d}-{context.period_start.month:02d}",
        period_start=context.period_start,
        period_end=context.period_end,
        metric_key="estimatedRevenue",
        value_kind="estimated",
        amount_native=parse_decimal_amount(context.amount_raw, metric_key="estimatedRevenue"),
        currency_code=context.currency,
        source_report_id=report_id,
        raw_payload=deepcopy(context.raw_row),
    )


# ============================================================================
# Purpose: Translate YouTube Reporting API estimated-revenue payloads into
#          ParsedSourceRow rows (one per input line). No live download.
# Database/ORM: None directly. ParsedSourceRow is the parser/repository
#               boundary.
# Standards: Source amount + currency preserved exactly. Deterministic
#            source_row_key via build_source_row_key. value_kind is
#            'estimated' because the Reporting API reports estimated
#            revenue, not settled payments.
# Blast Radius: No DB write. No graph projection impact detected.
# Connections:
#   - File: tests/connectors/_fixtures/youtube_reporting/ -> Synthetic
#     payloads consumed by parser tests.
# ============================================================================
class YouTubeReportingParser:
    source_system = "youtube_reporting"

    def parse(
        self,
        payload: dict[str, object],
        *,
        tenant_id: UUID,
    ) -> Iterable[ParsedSourceRow]:
        metadata = require_dict(payload, "report_metadata")
        report_id = require_str(metadata, "report_id")
        report_type = require_str(metadata, "report_type")

        for row in _reporting_rows(payload):
            yield _reporting_source_row(
                source_system=self.source_system,
                report_id=report_id,
                report_type=report_type,
                context=_reporting_row_context(row),
            )
