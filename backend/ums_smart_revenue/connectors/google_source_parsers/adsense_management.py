"""Parser for AdSense Management API report payloads.

Two report shapes share the parser: estimated earnings (value_kind =
'estimated', report_type = 'earnings_report') and payment/settled
(value_kind = 'settled', report_type = 'payment_report'). The shape is
distinguished by the metric column type: PAID_AMOUNT => settled,
everything else => estimated.
"""

from collections.abc import Iterable
from datetime import date
from typing import Final
from uuid import UUID

from ums_smart_revenue.connectors.google_source_parsers.base import (
    ParserError,
    parse_decimal_amount,
    require_dict,
    require_str,
)
from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
    build_source_row_key,
)
from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow

_SETTLED_METRICS: Final[frozenset[str]] = frozenset({"PAID_AMOUNT", "UNPAID_AMOUNT"})


# ============================================================================
# Purpose: Translate AdSense earnings + payment report payloads into
#          ParsedSourceRow rows.
# Database/ORM: None. ParsedSourceRow boundary.
# Standards: Source amount + currency preserved exactly. value_kind
#            distinguishes estimated earnings vs settled payments.
# Blast Radius: No DB write. No graph projection impact detected.
# Connections:
#   - File: tests/connectors/_fixtures/adsense_management/ -> Fixtures.
# ============================================================================
class AdSenseManagementParser:
    source_system = "adsense_management"

    def parse(
        self,
        payload: dict[str, object],
        *,
        tenant_id: UUID,
    ) -> Iterable[ParsedSourceRow]:
        request = require_dict(payload, "request")
        headers = payload.get("headers")
        rows = payload.get("rows")
        report_id = require_str(payload, "report_id")
        if not isinstance(headers, list):
            raise ParserError("headers must be a list")
        if not isinstance(rows, list):
            raise ParserError("rows must be a list")

        account_raw = require_str(request, "accountId")
        account_id = account_raw.removeprefix("accounts/")
        period_start = self._parse_iso_date(require_dict(require_dict(request, "dateRange"), "startDate"))
        period_end = self._parse_iso_date(require_dict(require_dict(request, "dateRange"), "endDate"))
        currency = require_str(request, "currencyCode")

        dim_names = [
            h["name"] for h in headers
            if isinstance(h, dict) and h.get("type") == "DIMENSION"
        ]
        metric_names = [
            h["name"] for h in headers
            if isinstance(h, dict) and h.get("type") == "METRIC_CURRENCY"
        ]
        report_type = "payment_report" if any(m in _SETTLED_METRICS for m in metric_names) else "earnings_report"
        default_value_kind = "settled" if report_type == "payment_report" else "estimated"

        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise ParserError("each rows[*] must be a dict with 'cells'")
            cells = raw_row.get("cells")
            if not isinstance(cells, list) or len(cells) != len(headers):
                raise ParserError("row.cells length must match headers")
            values = [cell.get("value") if isinstance(cell, dict) else None for cell in cells]
            dim_values = dict(zip(dim_names, values[:len(dim_names)], strict=True))
            metric_values = dict(zip(metric_names, values[len(dim_names):], strict=True))

            for metric_name, raw_value in metric_values.items():
                if not isinstance(raw_value, str):
                    raise ParserError(f"metric {metric_name} value must be a string")
                source_row_key = build_source_row_key(
                    source_system=self.source_system,
                    source_report_id=f"{report_id}|{metric_name}",
                    account_id=account_id,
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                    dimensions=dim_values,
                )
                yield ParsedSourceRow(
                    source_system=self.source_system,
                    source_row_key=source_row_key,
                    source_account_id=account_id,
                    content_owner_id=None,
                    youtube_channel_id=None,  # AdSense reports are account-scoped, not channel-scoped.
                    report_type=report_type,
                    report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                    period_start=period_start,
                    period_end=period_end,
                    metric_key=metric_name,
                    value_kind=default_value_kind,
                    amount_native=parse_decimal_amount(raw_value, metric_key=metric_name),
                    currency_code=currency,
                    source_report_id=report_id,
                    raw_payload={"dimensions": dim_values, "metric": metric_name, "value": raw_value},
                )

    @staticmethod
    def _parse_iso_date(d: dict[str, object]) -> date:
        year = d.get("year")
        month = d.get("month")
        day = d.get("day")
        if not all(isinstance(v, int) for v in (year, month, day)):
            raise ParserError("dateRange.{startDate,endDate} require int year/month/day")
        return date(year, month, day)  # type: ignore[arg-type]
