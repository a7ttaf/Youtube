"""Parser for YouTube Reporting API report payloads.

Consumes a pre-recorded payload shaped like the parser-friendly JSON
the connector would emit after converting the upstream CSV report into
a dict. Emits ParsedSourceRow instances with value_kind='estimated'.
"""

from collections.abc import Iterable
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
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ParserError("payload['rows'] must be a list")
        report_id = require_str(metadata, "report_id")
        report_type = require_str(metadata, "report_type")

        for row in rows:
            if not isinstance(row, dict):
                raise ParserError("each rows[*] must be a dict")
            line_index = require_int(row, "line_index")
            date_range = require_dict(row, "date_range")
            period_start = parse_iso_date(require_str(date_range, "start"), field="date_range.start")
            period_end = parse_iso_date(require_str(date_range, "end"), field="date_range.end")
            dimensions = require_dict(row, "dimensions")
            metrics = require_dict(row, "metrics")

            channel = dimensions.get("channel")
            content_owner = dimensions.get("content_owner")
            if not isinstance(channel, str):
                raise ParserError("dimensions.channel must be a string")
            # FIX: Fail closed if content_owner is missing or non-string. The
            # previous branch silently substituted "unknown" for
            # source_account_id, which risked mis-attributing source rows in
            # multi-CMS tenants. Per CLAUDE.md ("preserve every financial
            # number's source") we surface the bad payload instead.
            if not isinstance(content_owner, str):
                raise ParserError("dimensions.content_owner is required for YouTube Reporting rows")

            amount_raw = metrics.get("estimatedRevenue")
            if not isinstance(amount_raw, str):
                raise ParserError("metrics.estimatedRevenue must be a string for Decimal precision")
            currency = metrics.get("currencyCode")
            if not isinstance(currency, str):
                raise ParserError("metrics.currencyCode must be a string")

            source_row_key = build_source_row_key(
                source_system=self.source_system,
                source_report_id=report_id,
                line_index=line_index,
                dimensions=dimensions,
            )

            yield ParsedSourceRow(
                source_system=self.source_system,
                source_row_key=source_row_key,
                source_account_id=content_owner,
                content_owner_id=content_owner,
                youtube_channel_id=channel,
                report_type=report_type,
                report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                period_start=period_start,
                period_end=period_end,
                metric_key="estimatedRevenue",
                value_kind="estimated",
                amount_native=parse_decimal_amount(amount_raw, metric_key="estimatedRevenue"),
                currency_code=currency,
                source_report_id=report_id,
                raw_payload=dict(row),
            )
