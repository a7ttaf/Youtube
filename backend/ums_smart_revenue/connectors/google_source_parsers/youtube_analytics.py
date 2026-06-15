"""Parser for YouTube Analytics reports.query response payloads.

Consumes a `youtubeAnalytics#resultTable` shape: columnHeaders + rows.
Emits one ParsedSourceRow per monetary metric per data row. value_kind
is always 'estimated' because Analytics never returns settled values.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
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
    {
        "estimatedRevenue",
        "grossRevenue",
        "estimatedAdRevenue",
        "estimatedRedPartnerRevenue",
        "adRevenue",
    }
)


def _canonical_csv(value: str, sep: str) -> str:
    """Order-independent canonical form of a ``sep``-delimited list.

    Sorting the parts keeps source_row_key stable when a caller reorders the
    same metric/dimension/filter set across runs, preserving idempotency.
    """
    return sep.join(sorted(part.strip() for part in value.split(sep) if part.strip()))


def _canonical_filters(filters: str | None) -> str | None:
    """Order-independent canonical form of a YouTube Analytics filter string.

    An omitted filter and a present-but-empty filter both mean "unfiltered",
    so both normalise to None (identical row key). Clause order (``;``) and the
    order of comma-separated values inside an ``==`` clause (e.g. ``video==a,b``)
    are semantically irrelevant, so both are sorted. Clauses using other
    operators keep their content intact (only their position is sorted) to
    avoid incorrectly merging distinct filters.
    """
    if not filters:
        return None
    canonical_clauses: list[str] = []
    for raw_clause in filters.split(";"):
        clause = raw_clause.strip()
        if not clause:
            continue
        if "==" in clause:
            key, _, values = clause.partition("==")
            sorted_values = ",".join(sorted(v.strip() for v in values.split(",") if v.strip()))
            clause = f"{key.strip()}=={sorted_values}"
        canonical_clauses.append(clause)
    return ";".join(sorted(canonical_clauses)) or None


@dataclass(frozen=True)
class _AnalyticsParseContext:
    period_start: date
    period_end: date
    currency: str
    content_owner_id: str | None
    normalized_ids: str
    query_signature: str
    filters_key: str | None


# ============================================================================
# Purpose: Validate YouTube Analytics parser context and tabular row structure.
# Database/ORM: None. ParsedSourceRow boundary.
# Standards: Typed ParserError failures, stable source-row identity axes.
# Blast Radius: Finance ingestion provenance; no graph projection impact detected.
# Connections:
#   - File: tests/connectors/google_source_parsers/test_youtube_analytics_parser.py
#     -> Parser contract.
#   - File: backend/ums_smart_revenue/connectors/google_source_rows.py -> ParsedSourceRow shape.
# ============================================================================
def _analytics_parse_context(request: dict[str, object]) -> _AnalyticsParseContext:
    """Extract request-level YouTube Analytics parser context."""
    period_start = parse_iso_date(require_str(request, "startDate"), field="startDate")
    period_end = parse_iso_date(require_str(request, "endDate"), field="endDate")
    currency = _analytics_currency(request)
    include_historical = _analytics_include_historical(request)
    require_str(request, "metrics")
    dimensions_csv = require_str(request, "dimensions")
    content_owner_id, normalized_ids = _analytics_ids(require_str(request, "ids"))
    filters_key = _analytics_filters_key(request)
    _require_analytics_single_month_range(period_start, period_end)
    account_identity = content_owner_id or ""
    query_signature = (
        f"{account_identity}|{_canonical_csv(dimensions_csv, ',')}"
        f"|hist={'1' if include_historical else '0'}"
    )
    return _AnalyticsParseContext(
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        content_owner_id=content_owner_id,
        normalized_ids=normalized_ids,
        query_signature=query_signature,
        filters_key=filters_key,
    )


def _analytics_currency(request: dict[str, object]) -> str:
    """Return the requested currency, defaulting omitted Analytics currency to USD."""
    if "currency" not in request:
        return "USD"
    currency = request["currency"]
    if not isinstance(currency, str) or not currency.strip():
        raise ParserError("query_request.currency must be a non-empty string when present")
    return currency.strip()


def _analytics_include_historical(request: dict[str, object]) -> bool:
    """Validate includeHistoricalChannelData because it changes row-key identity."""
    include_historical = request.get("includeHistoricalChannelData", False)
    if not isinstance(include_historical, bool):
        raise ParserError(
            "query_request.includeHistoricalChannelData must be a boolean when present"
        )
    return include_historical


def _analytics_ids(ids: str) -> tuple[str | None, str]:
    """Normalize a structured reports.query ids selector."""
    selector_kind, sep, selector_id = ids.partition("==")
    selector_id = selector_id.strip()
    if sep != "==" or selector_kind not in {"channel", "contentOwner"} or not selector_id:
        raise ParserError(
            f"query_request.ids must be 'channel==<id>' or 'contentOwner==<id>', got {ids!r}"
        )
    content_owner_id = selector_id if selector_kind == "contentOwner" else None
    return content_owner_id, f"{selector_kind}=={selector_id}"


def _analytics_filters_key(request: dict[str, object]) -> str | None:
    """Validate and canonicalize optional Analytics filters for idempotent keys."""
    filters = request.get("filters")
    if filters is not None and not isinstance(filters, str):
        raise ParserError("query_request.filters must be a string when present")
    return _canonical_filters(filters)


def _require_analytics_single_month_range(period_start: date, period_end: date) -> None:
    """Fail closed when an Analytics range would mis-bucket report_month."""
    if period_end < period_start:
        raise ParserError(
            "reports.query endDate must be on or after startDate, got "
            f"{period_start.isoformat()}..{period_end.isoformat()}"
        )
    if (period_start.year, period_start.month) != (period_end.year, period_end.month):
        raise ParserError(
            "reports.query range must fall within a single calendar month for "
            f"report_month bucketing, got {period_start.isoformat()}..{period_end.isoformat()}"
        )


def _analytics_rows(payload: dict[str, object]) -> list[object]:
    """Return Analytics rows, treating omitted rows as a clean zero-result."""
    rows = payload.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ParserError("rows must be a list")
    return rows


def _analytics_header_specs(column_headers: object) -> list[tuple[str, str]]:
    """Validate column headers and keep declaration order for positional routing."""
    if not isinstance(column_headers, list):
        raise ParserError("columnHeaders must be a list")

    header_specs: list[tuple[str, str]] = []
    for header in column_headers:
        if not isinstance(header, dict):
            raise ParserError("each columnHeaders[*] must be an object")
        column_type = header.get("columnType")
        if column_type not in {"DIMENSION", "METRIC"}:
            raise ParserError(f"unsupported columnHeaders[*].columnType: {column_type!r}")
        name = header.get("name")
        if not isinstance(name, str):
            raise ParserError("each DIMENSION/METRIC header requires a string name")
        if (column_type, name) in header_specs:
            raise ParserError(f"duplicate columnHeaders entry: {column_type}.{name}")
        header_specs.append((column_type, name))
    return header_specs


def _analytics_row_values(
    data_row: object,
    header_specs: list[tuple[str, str]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Route one Analytics tabular row into dimension and metric maps."""
    if not isinstance(data_row, list):
        raise ParserError("each rows[*] must be a list (tabular)")
    if len(data_row) != len(header_specs):
        raise ParserError(f"row length {len(data_row)} != columnHeaders length {len(header_specs)}")

    dim_values: dict[str, object] = {}
    metric_values: dict[str, object] = {}
    for (column_type, name), value in zip(header_specs, data_row, strict=True):
        if column_type == "DIMENSION":
            dim_values[name] = value
        else:
            metric_values[name] = value
    return dim_values, metric_values


def _normalize_analytics_channel(dim_values: dict[str, object]) -> str:
    """Normalize the required channel attribution dimension in-place."""
    channel = dim_values.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise ParserError("dimensions.channel must be a non-empty string")
    channel = channel.strip()
    dim_values["channel"] = channel
    return channel


def _iter_analytics_source_rows(
    *,
    source_system: str,
    context: _AnalyticsParseContext,
    metric_names: list[str],
    dim_values: dict[str, object],
    metric_values: dict[str, object],
    channel: str,
) -> Iterable[ParsedSourceRow]:
    """Emit one ParsedSourceRow per monetary YouTube Analytics metric."""
    for metric_name in metric_names:
        if metric_name not in _MONETARY_METRICS:
            continue
        raw_value = metric_values[metric_name]
        source_row_key = build_source_row_key(
            source_system=source_system,
            query_signature=f"{context.query_signature}|{metric_name}",
            currency=context.currency,
            filters=context.filters_key,
            period_start=context.period_start.isoformat(),
            period_end=context.period_end.isoformat(),
            dimensions=dim_values,
        )
        yield ParsedSourceRow(
            source_system=source_system,
            source_row_key=source_row_key,
            source_account_id=context.normalized_ids,
            content_owner_id=context.content_owner_id,
            youtube_channel_id=channel,
            report_type="reports.query",
            report_month=f"{context.period_start.year:04d}-{context.period_start.month:02d}",
            period_start=context.period_start,
            period_end=context.period_end,
            metric_key=metric_name,
            value_kind="estimated",
            amount_native=parse_decimal_amount(raw_value, metric_key=metric_name),
            currency_code=context.currency,
            source_report_id=None,
            raw_payload={"dimensions": dict(dim_values), "metric": metric_name, "value": raw_value},
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
        context = _analytics_parse_context(request)
        header_specs = _analytics_header_specs(payload.get("columnHeaders"))
        metric_names = [name for column_type, name in header_specs if column_type == "METRIC"]

        for data_row in _analytics_rows(payload):
            dim_values, metric_values = _analytics_row_values(data_row, header_specs)
            channel = _normalize_analytics_channel(dim_values)
            yield from _iter_analytics_source_rows(
                source_system=self.source_system,
                context=context,
                metric_names=metric_names,
                dim_values=dim_values,
                metric_values=metric_values,
                channel=channel,
            )
