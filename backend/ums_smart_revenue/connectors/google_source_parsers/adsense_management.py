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
        # FIX: reports.generate omits `rows` entirely for a no-activity period;
        # treat a missing/null rows as a clean zero-result (emit nothing) rather
        # than failing ingestion for a valid empty report. Mirrors
        # YouTubeAnalyticsParser. A present non-list rows is still malformed.
        rows = payload.get("rows")
        if rows is None:
            rows = []
        report_id = require_str(payload, "report_id")
        if not isinstance(headers, list):
            raise ParserError("headers must be a list")
        if not isinstance(rows, list):
            raise ParserError("rows must be a list")

        account_raw = require_str(request, "accountId")
        # FIX: Fail closed on a malformed accountId instead of silently
        # normalising it. AdSense always returns "accounts/<id>"; a value
        # without the prefix (e.g. "pub-1") or with an empty suffix
        # ("accounts/") would produce an invalid source_account_id and weaken
        # source_row_key idempotency/key consistency.
        if not account_raw.startswith("accounts/"):
            raise ParserError("request.accountId must start with 'accounts/'")
        # FIX: strip the suffix before the non-empty check so a whitespace-only id
        # ("accounts/   ") fails closed too — it would otherwise pass the
        # truthiness check and persist a blank source_account_id / key axis.
        account_id = account_raw.removeprefix("accounts/").strip()
        if not account_id:
            raise ParserError(
                "request.accountId must include a non-empty account id after 'accounts/'"
            )
        period_start = self._parse_iso_date(
            require_dict(require_dict(request, "dateRange"), "startDate")
        )
        period_end = self._parse_iso_date(
            require_dict(require_dict(request, "dateRange"), "endDate")
        )
        currency = require_str(request, "currencyCode")
        # FIX: reject a blank/whitespace currency: it is not a valid ISO code and
        # would persist as a blank currency_code and key axis (mirrors the YouTube
        # Analytics currency guard; CLAUDE.md rule 4). Store the trimmed code; the
        # METRIC_CURRENCY header check below compares against this trimmed value.
        if not currency.strip():
            raise ParserError("request.currencyCode must be a non-empty string")
        currency = currency.strip()
        # Reject reversed ranges before month bucketing: endDate must be on or
        # after startDate. The DB has a period-order constraint, but fail closed
        # here so a malformed payload stays on the parser's typed contract.
        if period_end < period_start:
            raise ParserError(
                "dateRange.endDate must be on or after startDate, got "
                f"{period_start.isoformat()}..{period_end.isoformat()}"
            )
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
            if (
                header_type not in {"DIMENSION", "METRIC_CURRENCY"}
                and header_type not in _TOLERATED_METRIC_HEADER_TYPES
            ):
                raise ParserError(f"unsupported headers[*].type: {header_type!r}")
            name = h.get("name")
            if not isinstance(name, str):
                raise ParserError("each DIMENSION/METRIC_CURRENCY header requires a string name")
            if (header_type, name) in header_specs:
                # Duplicate header would overwrite the earlier cell in
                # dim_values/metric_values and silently corrupt the row mapping.
                raise ParserError(f"duplicate headers entry: {header_type}.{name}")
            # FIX: A METRIC_CURRENCY header declares the ISO currency of the
            # amounts in its column. The parser stamps request.currencyCode on
            # every emitted row, so a header whose currencyCode diverges from
            # the request would persist a preserved amount under the wrong ISO
            # code and corrupt downstream finance totals (CLAUDE.md rule 4).
            # AdSense returns monetary metrics in the requested currency, so
            # fail closed on a missing/non-string/mismatched header currency.
            if header_type == "METRIC_CURRENCY":
                header_currency = h.get("currencyCode")
                if not isinstance(header_currency, str):
                    raise ParserError(
                        f"METRIC_CURRENCY header {name!r} requires a string currencyCode"
                    )
                if header_currency != currency:
                    raise ParserError(
                        f"METRIC_CURRENCY header {name!r} currencyCode "
                        f"{header_currency!r} does not match request currencyCode "
                        f"{currency!r}"
                    )
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
                if not isinstance(cell, dict) or "value" not in cell:
                    raise ParserError("each row.cells[*] must be an object containing 'value'")
                value = cell["value"]
                if header_type == "DIMENSION":
                    dim_values[name] = value
                elif header_type == "METRIC_CURRENCY":
                    metric_values[name] = value
                # else: tolerated non-currency metric — consumed positionally to
                # keep cell alignment, but not emitted as a monetary row.

            for metric_name, raw_value in metric_values.items():
                if not isinstance(raw_value, str):
                    raise ParserError(f"metric {metric_name} value must be a string")
                # Derive value_kind + report_type per metric, not once per
                # report: a single AdSense report can mix settled (PAID_AMOUNT)
                # and estimated metrics. A report-level any()-based label would
                # tag estimated rows as 'settled' and corrupt downstream
                # payment-vs-estimate interpretation.
                is_settled = metric_name in _SETTLED_METRICS
                value_kind = "settled" if is_settled else "estimated"
                report_type = "payment_report" if is_settled else "earnings_report"
                # FIX: key on the stable logical identity (metric + account +
                # period + dimensions), NOT the run-specific report_id. report_id
                # is preserved as source_report_id provenance below, but folding
                # it into the dedup key let a regenerated report (new report_id)
                # for the same month/account/dimensions insert duplicate revenue
                # rows on rerun instead of updating in place.
                source_row_key = build_source_row_key(
                    source_system=self.source_system,
                    metric_key=metric_name,
                    # currency is part of the logical identity: the same
                    # metric/account/period/dimensions in a different currency is
                    # a distinct monetary row and must not collide on the key.
                    currency=currency,
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
                    # AdSense reports are account-scoped, not channel-scoped.
                    youtube_channel_id=None,
                    report_type=report_type,
                    report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                    period_start=period_start,
                    period_end=period_end,
                    metric_key=metric_name,
                    value_kind=value_kind,
                    amount_native=parse_decimal_amount(raw_value, metric_key=metric_name),
                    currency_code=currency,
                    source_report_id=report_id,
                    # FIX: store an OWNED copy of dim_values per yielded row. Every
                    # monetary metric from one input row shares the same dim_values
                    # object, so persisting it by reference would let a later
                    # mutation of one row's raw_payload["dimensions"] silently
                    # corrupt sibling metric rows' audit payloads (CLAUDE.md rule
                    # 4). Cells are scalars, so a shallow dict() copy isolates it.
                    raw_payload={
                        "dimensions": dict(dim_values),
                        "metric": metric_name,
                        "value": raw_value,
                    },
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
