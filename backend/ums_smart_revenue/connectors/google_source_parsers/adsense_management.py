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
        # report_month is derived from period_start, so a dateRange spanning
        # more than one calendar month would mis-bucket every row; fail closed.
        if (period_start.year, period_start.month) != (period_end.year, period_end.month):
            raise ParserError(
                "dateRange must fall within a single calendar month for "
                f"report_month bucketing, got {period_start.isoformat()}..{period_end.isoformat()}"
            )

        # Keep each header's (type, name) in declaration order so row cells can
        # be routed positionally (cells[i] belongs to headers[i]). An
        # unsupported header type fails closed: skipping it would shift every
        # later cell into the wrong column and silently mislabel revenue values
        # (e.g. ingest a METRIC_TALLY click count as a currency amount). A typed
        # header missing its name likewise raises ParserError, not KeyError.
        header_specs: list[tuple[str, str]] = []
        for h in headers:
            if not isinstance(h, dict):
                raise ParserError("each headers[*] must be an object")
            header_type = h.get("type")
            if header_type not in {"DIMENSION", "METRIC_CURRENCY"}:
                raise ParserError(f"unsupported headers[*].type: {header_type!r}")
            name = h.get("name")
            if not isinstance(name, str):
                raise ParserError("each DIMENSION/METRIC_CURRENCY header requires a string name")
            header_specs.append((header_type, name))

        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise ParserError("each rows[*] must be a dict with 'cells'")
            cells = raw_row.get("cells")
            if not isinstance(cells, list) or len(cells) != len(header_specs):
                raise ParserError("row.cells length must match headers")
            # Route each cell to its header by position; only METRIC_CURRENCY
            # cells become monetary amounts.
            dim_values: dict[str, object] = {}
            metric_values: dict[str, object] = {}
            for (header_type, name), cell in zip(header_specs, cells, strict=True):
                value = cell.get("value") if isinstance(cell, dict) else None
                if header_type == "DIMENSION":
                    dim_values[name] = value
                else:
                    metric_values[name] = value

            for metric_name, raw_value in metric_values.items():
                if not isinstance(raw_value, str):
                    raise ParserError(f"metric {metric_name} value must be a string")
                # Derive value_kind + report_type per metric, not once per
                # report: a single AdSense report can mix settled (PAID/UNPAID)
                # and estimated metrics. A report-level any()-based label would
                # tag estimated rows as 'settled' and corrupt downstream
                # payment-vs-estimate interpretation.
                is_settled = metric_name in _SETTLED_METRICS
                value_kind = "settled" if is_settled else "estimated"
                report_type = "payment_report" if is_settled else "earnings_report"
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
                    value_kind=value_kind,
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
        # bool is a subclass of int; exclude it so True/False can't slip
        # through as 1/0 calendar components.
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (year, month, day)):
            raise ParserError("dateRange.{startDate,endDate} require int year/month/day")
        try:
            return date(year, month, day)  # type: ignore[arg-type]
        except ValueError as exc:
            # date() raises ValueError for out-of-range calendar values
            # (e.g. month 13, day 32); surface it as the typed ParserError
            # so malformed payloads stay on the parser's failure contract.
            raise ParserError(
                f"dateRange has invalid calendar date: {year:04d}-{month:02d}-{day:02d}"
            ) from exc
