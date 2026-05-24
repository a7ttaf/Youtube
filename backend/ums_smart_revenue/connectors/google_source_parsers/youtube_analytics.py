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
            sorted_values = ",".join(
                sorted(v.strip() for v in values.split(",") if v.strip())
            )
            clause = f"{key.strip()}=={sorted_values}"
        canonical_clauses.append(clause)
    return ";".join(sorted(canonical_clauses)) or None


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
        # reports.query omits `rows` entirely when there is no data for the
        # range; treat a missing/null rows as a clean zero-result (emit nothing)
        # rather than failing ingestion for a sparse channel. A present
        # non-list rows is still malformed.
        rows = payload.get("rows")
        if rows is None:
            rows = []
        if not isinstance(column_headers, list):
            raise ParserError("columnHeaders must be a list")
        if not isinstance(rows, list):
            raise ParserError("rows must be a list")

        period_start = parse_iso_date(require_str(request, "startDate"), field="startDate")
        period_end = parse_iso_date(require_str(request, "endDate"), field="endDate")
        currency = require_str(request, "currency")
        # `metrics` is validated for payload shape but intentionally NOT part of
        # the row key: the parser persists only monetary metrics and tags each
        # row with its own metric_key, so co-requesting an extra metric (e.g.
        # estimatedRevenue vs estimatedRevenue,views) must not change the key of
        # an existing revenue row — otherwise the rerun inserts duplicates.
        require_str(request, "metrics")
        dimensions_csv = require_str(request, "dimensions")
        ids = require_str(request, "ids")
        # FIX: Fail closed on a malformed `ids` selector. The YouTube Analytics
        # API contract requires a structured selector — `channel==<id>` or
        # `contentOwner==<id>`. Arbitrary text (unprefixed garbage, or a bare
        # `contentOwner==`/`channel==` with an empty id) would otherwise persist
        # a revenue row whose source_account_id/content_owner_id cannot be tied
        # back to a real owner or channel, weakening account attribution.
        # Mirrors the AdSense parser's accountId prefix/suffix guard.
        selector_kind, sep, selector_id = ids.partition("==")
        if sep != "==" or selector_kind not in {"channel", "contentOwner"} or not selector_id:
            raise ParserError(
                "query_request.ids must be 'channel==<id>' or 'contentOwner==<id>', "
                f"got {ids!r}"
            )
        # content_owner_id holds the BARE id (the part after "==") so it is
        # usable for joins/filters; source_account_id keeps the raw `ids`
        # selector. A channel-scoped selector has no content owner.
        content_owner_id = selector_id if selector_kind == "contentOwner" else None
        # FIX: Include `ids` in query_signature so two payloads identical except
        # for the contentOwner/channel account produce distinct source_row_keys.
        # Without this, the repo PK (tenant_id, source_system, source_row_key)
        # silently collapses cross-account data in a multi-CMS tenant.
        # Canonicalise (sort) the dimension list so a rerun that only reorders
        # it hashes to the same key. Metrics are deliberately excluded (see the
        # require_str note above); each row's own metric is folded in per-row
        # via metric_name below.
        query_signature = f"{ids}|{_canonical_csv(dimensions_csv, ',')}"
        # A different currency or filter expression for the same
        # ids/metrics/dimensions/period is a distinct dataset; fold both into
        # the row key (as structured fields, below) so one cannot overwrite the
        # other on the unique upsert key. filters is optional in the request.
        filters = request.get("filters")
        if filters is not None and not isinstance(filters, str):
            raise ParserError("query_request.filters must be a string when present")
        # Canonicalise filters for idempotency: an omitted filter and a
        # present-but-empty filter both mean "unfiltered" (normalise to None),
        # and clause / comma-separated multi-value order is semantically
        # irrelevant (sorted) — see _canonical_filters.
        filters_key = _canonical_filters(filters)
        # Reject reversed ranges before month bucketing: endDate must be on or
        # after startDate; fail closed so a malformed payload stays typed.
        if period_end < period_start:
            raise ParserError(
                "reports.query endDate must be on or after startDate, got "
                f"{period_start.isoformat()}..{period_end.isoformat()}"
            )
        # report_month is derived from period_start, so a range spanning more
        # than one calendar month would mis-bucket every returned row; fail
        # closed and require single-month queries.
        if (period_start.year, period_start.month) != (period_end.year, period_end.month):
            raise ParserError(
                "reports.query range must fall within a single calendar month for "
                f"report_month bucketing, got {period_start.isoformat()}..{period_end.isoformat()}"
            )

        # Keep each header's (type, name) in declaration order so row cells can
        # be routed positionally (data_row[i] belongs to columnHeaders[i]). An
        # unsupported columnType fails closed: silently skipping it would leave
        # the positional routing misaligned and could associate a value with
        # the wrong metric. A typed header missing its name likewise raises
        # ParserError, not KeyError.
        header_specs: list[tuple[str, str]] = []
        for h in column_headers:
            if not isinstance(h, dict):
                raise ParserError("each columnHeaders[*] must be an object")
            column_type = h.get("columnType")
            if column_type not in {"DIMENSION", "METRIC"}:
                raise ParserError(f"unsupported columnHeaders[*].columnType: {column_type!r}")
            name = h.get("name")
            if not isinstance(name, str):
                raise ParserError("each DIMENSION/METRIC header requires a string name")
            if (column_type, name) in header_specs:
                # Duplicate header would overwrite the earlier cell in
                # dim_values/metric_values and silently corrupt the row mapping.
                raise ParserError(f"duplicate columnHeaders entry: {column_type}.{name}")
            header_specs.append((column_type, name))

        metric_names = [name for column_type, name in header_specs if column_type == "METRIC"]

        for data_row in rows:
            if not isinstance(data_row, list):
                raise ParserError("each rows[*] must be a list (tabular)")
            if len(data_row) != len(header_specs):
                raise ParserError(
                    f"row length {len(data_row)} != columnHeaders length {len(header_specs)}"
                )

            # Route each value to its header by position; only DIMENSION values
            # populate dim_values and only METRIC values populate metric_values.
            dim_values: dict[str, object] = {}
            metric_values: dict[str, object] = {}
            for (column_type, name), value in zip(header_specs, data_row, strict=True):
                if column_type == "DIMENSION":
                    dim_values[name] = value
                else:
                    metric_values[name] = value

            channel = dim_values.get("channel")
            if not isinstance(channel, str):
                raise ParserError("dimensions.channel must be a string")

            for metric_name in metric_names:
                if metric_name not in _MONETARY_METRICS:
                    continue  # B1 only tracks monetary metrics.
                raw_value = metric_values[metric_name]
                # FIX: reports.query returns FLOAT/INTEGER metric cells as JSON
                # numbers (per columnHeaders[].dataType), not strings. The prior
                # str-only guard raised ParserError on valid Analytics payloads,
                # blocking ingestion. parse_decimal_amount now safely normalises
                # str/int/float (rejecting bool and other types), so numeric
                # cells parse correctly and the guard is removed.

                source_row_key = build_source_row_key(
                    source_system=self.source_system,
                    query_signature=f"{query_signature}|{metric_name}",
                    currency=currency,
                    filters=filters_key,
                    period_start=period_start.isoformat(),
                    period_end=period_end.isoformat(),
                    dimensions=dim_values,
                )

                yield ParsedSourceRow(
                    source_system=self.source_system,
                    source_row_key=source_row_key,
                    source_account_id=ids,
                    content_owner_id=content_owner_id,
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
