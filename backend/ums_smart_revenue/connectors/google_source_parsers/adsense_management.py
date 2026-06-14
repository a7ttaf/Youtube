"""Parser for AdSense Management API report payloads.

Two report shapes share the parser: estimated earnings (value_kind =
'estimated', report_type = 'earnings_report') and payment/settled
(value_kind = 'settled', report_type = 'payment_report'). The shape is
distinguished by the metric column type: PAID_AMOUNT => settled,
everything else => estimated.
"""

from collections.abc import Iterable
from dataclasses import dataclass
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

# FIX: UNPAID_AMOUNT is outstanding (not-yet-paid) earnings, not settled cash;
# the module contract above states only PAID_AMOUNT is settled. Tagging
# UNPAID_AMOUNT as value_kind='settled'/'payment_report' would mislabel an
# unpaid balance as finalized cash and corrupt finalized-vs-estimated totals
# and payment reconciliation (CLAUDE.md rule 4). Only PAID_AMOUNT is settled.
_SETTLED_METRICS: Final[frozenset[str]] = frozenset({"PAID_AMOUNT"})

# FIX: AdSense ReportResult.HeaderType includes non-currency metric types that a
# report may carry alongside the currency metrics B1 ingests. Tolerate them —
# route their cells positionally so column alignment is preserved, but do NOT
# emit rows for them — instead of failing the whole report, which would drop the
# parseable currency revenue. A truly unknown header type still fails closed (no
# silent mis-routing).
_TOLERATED_METRIC_HEADER_TYPES: Final[frozenset[str]] = frozenset(
    {"METRIC_TALLY", "METRIC_RATIO", "METRIC_MILLISECONDS", "METRIC_DECIMAL"}
)


@dataclass(frozen=True)
class _AdSenseParseContext:
    account_id: str
    report_id: str
    period_start: date
    period_end: date
    currency: str


# ============================================================================
# Purpose: Validate AdSense parser payload structure before emitting source rows.
# Database/ORM: None. ParsedSourceRow boundary.
# Standards: Typed ParserError failures, stable source-row identity axes.
# Blast Radius: Finance ingestion provenance; no graph projection impact detected.
# Connections:
#   - File: tests/connectors/google_source_parsers/test_adsense_management_parser.py -> Parser contract.
#   - File: backend/ums_smart_revenue/connectors/google_source_rows.py -> ParsedSourceRow shape.
# ============================================================================
def _adsense_parse_context(payload: dict[str, object]) -> _AdSenseParseContext:
    """Extract request-level AdSense parser context from a validated payload."""
    request = require_dict(payload, "request")
    account_id = _adsense_account_id(require_str(request, "accountId"))
    period_start = _parse_adsense_iso_date(require_dict(require_dict(request, "dateRange"), "startDate"))
    period_end = _parse_adsense_iso_date(require_dict(require_dict(request, "dateRange"), "endDate"))
    currency = _adsense_currency(require_str(request, "currencyCode"))
    _require_single_month_range(period_start, period_end, prefix="dateRange")
    return _AdSenseParseContext(
        account_id=account_id,
        report_id=require_str(payload, "report_id"),
        period_start=period_start,
        period_end=period_end,
        currency=currency,
    )


def _adsense_account_id(account_raw: str) -> str:
    """Return the bare AdSense account id from the required accounts/<id> resource."""
    if not account_raw.startswith("accounts/"):
        raise ParserError("request.accountId must start with 'accounts/'")
    account_id = account_raw.removeprefix("accounts/").strip()
    if not account_id:
        raise ParserError("request.accountId must include a non-empty account id after 'accounts/'")
    return account_id


def _adsense_currency(currency: str) -> str:
    """Trim and validate the requested AdSense report currency."""
    if not currency.strip():
        raise ParserError("request.currencyCode must be a non-empty string")
    return currency.strip()


def _require_single_month_range(period_start: date, period_end: date, *, prefix: str) -> None:
    """Fail closed when a parser payload would mis-bucket report_month."""
    if period_end < period_start:
        raise ParserError(
            f"{prefix}.endDate must be on or after startDate, got "
            f"{period_start.isoformat()}..{period_end.isoformat()}"
        )
    if (period_start.year, period_start.month) != (period_end.year, period_end.month):
        raise ParserError(
            f"{prefix} must fall within a single calendar month for "
            f"report_month bucketing, got {period_start.isoformat()}..{period_end.isoformat()}"
        )


def _adsense_rows(payload: dict[str, object]) -> list[object]:
    """Return AdSense report rows, treating omitted rows as a clean zero-result."""
    rows = payload.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ParserError("rows must be a list")
    return rows


def _adsense_header_specs(headers: object, currency: str) -> list[tuple[str, str]]:
    """Validate headers and keep declaration order for positional cell routing."""
    if not isinstance(headers, list):
        raise ParserError("headers must be a list")

    header_specs: list[tuple[str, str]] = []
    for header in headers:
        if not isinstance(header, dict):
            raise ParserError("each headers[*] must be an object")
        header_type = header.get("type")
        if (
            header_type not in {"DIMENSION", "METRIC_CURRENCY"}
            and header_type not in _TOLERATED_METRIC_HEADER_TYPES
        ):
            raise ParserError(f"unsupported headers[*].type: {header_type!r}")
        name = header.get("name")
        if not isinstance(name, str):
            raise ParserError("each DIMENSION/METRIC_CURRENCY header requires a string name")
        if (header_type, name) in header_specs:
            raise ParserError(f"duplicate headers entry: {header_type}.{name}")
        if header_type == "METRIC_CURRENCY":
            _require_adsense_header_currency(header, name=name, request_currency=currency)
        header_specs.append((header_type, name))
    return header_specs


def _require_adsense_header_currency(
    header: dict[object, object],
    *,
    name: str,
    request_currency: str,
) -> None:
    """Ensure a monetary header preserves the same ISO currency stamped on rows."""
    header_currency = header.get("currencyCode")
    if not isinstance(header_currency, str):
        raise ParserError(f"METRIC_CURRENCY header {name!r} requires a string currencyCode")
    if header_currency != request_currency:
        raise ParserError(
            f"METRIC_CURRENCY header {name!r} currencyCode {header_currency!r} "
            f"does not match request currencyCode {request_currency!r}"
        )


def _adsense_cell_values(
    raw_row: object,
    header_specs: list[tuple[str, str]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Route one AdSense row's cells to dimension and monetary metric maps."""
    if not isinstance(raw_row, dict):
        raise ParserError("each rows[*] must be a dict with 'cells'")
    cells = raw_row.get("cells")
    if not isinstance(cells, list) or len(cells) != len(header_specs):
        raise ParserError("row.cells length must match headers")

    dim_values: dict[str, object] = {}
    metric_values: dict[str, object] = {}
    for (header_type, name), cell in zip(header_specs, cells, strict=True):
        if not isinstance(cell, dict) or "value" not in cell:
            raise ParserError("each row.cells[*] must be an object containing 'value'")
        value = cell["value"]
        if header_type == "DIMENSION":
            dim_values[name] = value
        elif header_type == "METRIC_CURRENCY":
            metric_values[name] = value
    return dim_values, metric_values


def _iter_adsense_source_rows(
    *,
    source_system: str,
    context: _AdSenseParseContext,
    dim_values: dict[str, object],
    metric_values: dict[str, object],
) -> Iterable[ParsedSourceRow]:
    """Emit one ParsedSourceRow per monetary AdSense metric cell."""
    for metric_name, raw_value in metric_values.items():
        if not isinstance(raw_value, str):
            raise ParserError(f"metric {metric_name} value must be a string")
        is_settled = metric_name in _SETTLED_METRICS
        value_kind = "settled" if is_settled else "estimated"
        report_type = "payment_report" if is_settled else "earnings_report"
        source_row_key = build_source_row_key(
            source_system=source_system,
            metric_key=metric_name,
            currency=context.currency,
            account_id=context.account_id,
            period_start=context.period_start.isoformat(),
            period_end=context.period_end.isoformat(),
            dimensions=dim_values,
        )
        yield ParsedSourceRow(
            source_system=source_system,
            source_row_key=source_row_key,
            source_account_id=context.account_id,
            content_owner_id=None,
            youtube_channel_id=None,
            report_type=report_type,
            report_month=f"{context.period_start.year:04d}-{context.period_start.month:02d}",
            period_start=context.period_start,
            period_end=context.period_end,
            metric_key=metric_name,
            value_kind=value_kind,
            amount_native=parse_decimal_amount(raw_value, metric_key=metric_name),
            currency_code=context.currency,
            source_report_id=context.report_id,
            raw_payload={"dimensions": dict(dim_values), "metric": metric_name, "value": raw_value},
        )


def _parse_adsense_iso_date(d: dict[str, object]) -> date:
    year = d.get("year")
    month = d.get("month")
    day = d.get("day")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (year, month, day)):
        raise ParserError("dateRange.{startDate,endDate} require int year/month/day")
    try:
        return date(year, month, day)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ParserError(
            f"dateRange has invalid calendar date: {year:04d}-{month:02d}-{day:02d}"
        ) from exc


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
        context = _adsense_parse_context(payload)
        header_specs = _adsense_header_specs(payload.get("headers"), context.currency)

        for raw_row in _adsense_rows(payload):
            dim_values, metric_values = _adsense_cell_values(raw_row, header_specs)
            yield from _iter_adsense_source_rows(
                source_system=self.source_system,
                context=context,
                dim_values=dim_values,
                metric_values=metric_values,
            )

    @staticmethod
    def _parse_iso_date(d: dict[str, object]) -> date:
        return _parse_adsense_iso_date(d)
