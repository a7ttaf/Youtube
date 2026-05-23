"""Parser for YouTube Analytics reports.query response payloads.

Consumes a `youtubeAnalytics#resultTable` shape: columnHeaders + rows.
Emits one ParsedSourceRow per monetary metric per data row. value_kind
is always 'estimated' because Analytics never returns settled values.
"""

from collections.abc import Iterable
from typing import Final
from uuid import UUID

from ums_smart_revenue.connectors.google_source_parsers.base import (
    ParserError,
    parse_decimal_amount,
    parse_iso_date,
    require_dict,
    require_str,
)
from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
    build_source_row_key,
)
from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow

_MONETARY_METRICS: Final[frozenset[str]] = frozenset(
    {"estimatedRevenue", "grossRevenue", "estimatedAdRevenue",
     "estimatedRedPartnerRevenue", "adRevenue"}
)


# ============================================================================
# Purpose: Translate YouTube Analytics reports.query payloads into
#          ParsedSourceRow rows, one per monetary metric per data row.
# Database/ORM: None. ParsedSourceRow boundary.
# Standards: Source amount + query currency preserved exactly.
#            value_kind='estimated' (Analytics never returns settled).
# Blast Radius: No DB write. No graph projection impact detected.
# Connections:
#   - File: tests/connectors/_fixtures/youtube_analytics/ -> Fixtures.
# ============================================================================
class YouTubeAnalyticsParser:
    source_system = "youtube_analytics"

    def parse(
        self,
        payload: dict[str, object],
        *,
        tenant_id: UUID,
    ) -> Iterable[ParsedSourceRow]:
        request = require_dict(payload, "query_request")
        column_headers = payload.get("columnHeaders")
        rows = payload.get("rows")
        if not isinstance(column_headers, list):
            raise ParserError("columnHeaders must be a list")
        if not isinstance(rows, list):
            raise ParserError("rows must be a list")

        period_start = parse_iso_date(require_str(request, "startDate"), field="startDate")
        period_end = parse_iso_date(require_str(request, "endDate"), field="endDate")
        currency = require_str(request, "currency")
        metrics_csv = require_str(request, "metrics")
        dimensions_csv = require_str(request, "dimensions")
        ids = require_str(request, "ids")
        # FIX: Include `ids` in query_signature so two payloads identical except
        # for the contentOwner/channel account produce distinct source_row_keys.
        # Without this, the repo PK (tenant_id, source_system, source_row_key)
        # silently collapses cross-account data in a multi-CMS tenant.
        query_signature = f"{ids}|{metrics_csv}|{dimensions_csv}"

        # Validate header shape before indexing: a typed header missing its
        # name must raise the parser's typed ParserError, not a bare KeyError.
        dimension_names: list[str] = []
        metric_names: list[str] = []
        for h in column_headers:
            if not isinstance(h, dict):
                raise ParserError("each columnHeaders[*] must be an object")
            column_type = h.get("columnType")
            if column_type not in {"DIMENSION", "METRIC"}:
                continue
            name = h.get("name")
            if not isinstance(name, str):
                raise ParserError("each DIMENSION/METRIC header requires a string name")
            if column_type == "DIMENSION":
                dimension_names.append(name)
            else:
                metric_names.append(name)

        for data_row in rows:
            if not isinstance(data_row, list):
                raise ParserError("each rows[*] must be a list (tabular)")
            if len(data_row) != len(column_headers):
                raise ParserError(
                    f"row length {len(data_row)} != columnHeaders length {len(column_headers)}"
                )

            dim_values = dict(zip(dimension_names, data_row[:len(dimension_names)], strict=True))
            metric_values = dict(zip(metric_names, data_row[len(dimension_names):], strict=True))

            channel = dim_values.get("channel")
            if not isinstance(channel, str):
                raise ParserError("dimensions.channel must be a string")

            for metric_name in metric_names:
                if metric_name not in _MONETARY_METRICS:
                    continue  # B1 only tracks monetary metrics.
                raw_value = metric_values[metric_name]
                if not isinstance(raw_value, str):
                    raise ParserError(
                        f"metric {metric_name} value must be a string for Decimal precision"
                    )

                source_row_key = build_source_row_key(
                    source_system=self.source_system,
                    query_signature=f"{query_signature}|{metric_name}",
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                    dimensions=dim_values,
                )

                yield ParsedSourceRow(
                    source_system=self.source_system,
                    source_row_key=source_row_key,
                    source_account_id=ids,
                    content_owner_id=ids if ids.startswith("contentOwner==") else None,
                    youtube_channel_id=channel,
                    report_type="reports.query",
                    report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                    period_start=period_start,
                    period_end=period_end,
                    metric_key=metric_name,
                    value_kind="estimated",
                    amount_native=parse_decimal_amount(raw_value, metric_key=metric_name),
                    currency_code=currency,
                    source_report_id=None,
                    raw_payload={"dimensions": dim_values, "metric": metric_name, "value": raw_value},
                )
