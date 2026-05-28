"""AdSense Management v2 reports.generate client + parser adapter (spec §5.6).

Endpoint: GET https://adsense.googleapis.com/v2/accounts/{account}/reports:generate
Query params (locked at ship):
    dateRange=CUSTOM
    startDate.{year,month,day}=...   <- first day of report_month
    endDate.{year,month,day}=...     <- last day of report_month (monthrange)
    dimensions=MONTH
    metrics[]=ESTIMATED_EARNINGS
    metrics[]=TOTAL_EARNINGS
    currencyCode=USD                 <- B2.6 ingests USD only; non-USD
                                        handling lives in C1's NON_USD_CURRENCY
                                        skip path, not here.

AdSenseManagementParser (PR #43 contract) requires a 'report_id' on the
payload, but AdSense reports.generate does not return a stable report id.
The adapter computes a deterministic SHA-256 of
(account_id, report_month, locked-report-key) and stamps it on the wrapped
payload so a rerun for the same (account, month) idempotently yields the
same source_report_id provenance.

AdSense data is ingestion/audit evidence only in B2. C1 skips AdSense rows
as SkipReason.MISSING_CHANNEL_ID until a future allocation/mapping spec.
"""
from __future__ import annotations

import hashlib
import re
from calendar import monthrange

from ums_smart_revenue.connectors.google.errors import (
    GoogleApiResponseError,
    MalformedAdsenseAccountIdError,
    MalformedReportMonthError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

_REPORT_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

_BASE = "https://adsense.googleapis.com/v2"
# Locked official AdSense v2 metric pair. Query params are sent as repeated
# ``metrics`` entries; a comma-delimited string is not accepted by the API.
_METRICS = ("ESTIMATED_EARNINGS", "TOTAL_EARNINGS")
# MONTH dimension only: B2.6 aggregates per-account-per-month; cell routing
# is positional so adding dimensions here without a parser update would
# silently mis-route monetary cells.
_DIMENSIONS = "MONTH"
# B2.6 USD-only locked. Non-USD handling is a C1 SkipReason concern.
_CURRENCY = "USD"

# Single source of truth for the report-kind key folded into the deterministic
# report_id stamp. Locked at ship: widening this set requires both a parser
# contract update and a new adapter branch.
SUPPORTED_ADSENSE_REPORTS: frozenset[str] = frozenset({"monthly_account_earnings"})
_REPORT_KEY = "monthly_account_earnings"
_MAX_REPORT_RESULT_ROWS = 100_000
_ACCOUNT_RESOURCE_PREFIX = "accounts/"
_ACCOUNT_ID_RESERVED_CHARS = frozenset("/?#%")


def _report_month_date_range(report_month: str) -> tuple[int, int, int]:
    """Return parsed year/month/last-day bounds for a validated report_month."""
    if not _REPORT_MONTH_PATTERN.fullmatch(report_month):
        # Fail closed before computing date bounds: `split("-")` on a
        # malformed value would raise a bare ValueError and escape the
        # GoogleConnectorError contract the orchestrator catches.
        raise MalformedReportMonthError(report_month=report_month)
    year_s, month_s = report_month.split("-")
    year_i, month_i = int(year_s), int(month_s)
    return year_i, month_i, monthrange(year_i, month_i)[1]


def _validated_account_id(account_id: str) -> str:
    """Return a normalized account id after fail-closed path validation."""
    candidate = account_id.strip()
    if not candidate or candidate != account_id:
        # Fail closed before URL construction so a blank or whitespace-padded
        # external account identifier cannot produce an ambiguous Google path.
        raise MalformedAdsenseAccountIdError(account_id=account_id)
    if candidate.startswith(_ACCOUNT_RESOURCE_PREFIX):
        candidate = candidate.removeprefix(_ACCOUNT_RESOURCE_PREFIX)
    if not candidate or any(ch in candidate for ch in _ACCOUNT_ID_RESERVED_CHARS):
        # Accept persisted full resource names ("accounts/<id>") by stripping
        # the trusted prefix, then reject every remaining path/query delimiter
        # before interpolating into the Google endpoint path.
        raise MalformedAdsenseAccountIdError(account_id=account_id)
    return candidate


def _synthesized_request(
    *, account_id: str, report_month: str,
) -> dict[str, object]:
    """Build parser-visible request metadata from trusted client inputs."""
    year_i, month_i, last_day = _report_month_date_range(report_month)
    return {
        "accountId": f"accounts/{account_id}",
        "dateRange": {
            "startDate": {"year": year_i, "month": month_i, "day": 1},
            "endDate": {"year": year_i, "month": month_i, "day": last_day},
        },
        "dimensions": [_DIMENSIONS],
        "metrics": list(_METRICS),
        "currencyCode": _CURRENCY,
    }


def _result_rows(*, response_json: dict[str, object], url: str) -> list[object] | None:
    """Return the optional ReportResult rows list after shape validation."""
    rows = response_json.get("rows")
    if rows is None:
        return None
    if isinstance(rows, list):
        return rows
    raise GoogleApiResponseError(
        url=url, reason=f"expected rows list or null, got {type(rows).__name__}"
    )


def _total_matched_rows(
    *, response_json: dict[str, object], url: str,
) -> int | None:
    """Return totalMatchedRows as an int when Google includes the count."""
    total = response_json.get("totalMatchedRows")
    if total is None:
        return None
    if isinstance(total, bool):
        raise GoogleApiResponseError(
            url=url, reason="expected totalMatchedRows integer, got bool"
        )
    if isinstance(total, int):
        if total < 0:
            raise GoogleApiResponseError(
                url=url, reason="totalMatchedRows must not be negative"
            )
        return total
    if isinstance(total, str) and total.isdecimal():
        return int(total)
    raise GoogleApiResponseError(
        url=url,
        reason=f"expected totalMatchedRows integer string, got {type(total).__name__}",
    )


# ============================================================================
# Purpose: Fail closed when AdSense reports.generate returns a partial
#   ReportResult. Google exposes totalMatchedRows separately from the returned
#   rows array; when the count is larger than returned rows, ingesting the body
#   would silently drop source evidence.
# Database/ORM: None (pure API response validation before parser shaping).
# Standards: Typed GoogleApiResponseError with safe aggregate counts only; no
#   raw row cells or secrets are included in diagnostics.
# Blast Radius: Connector ingestion evidence. Prevents partial AdSense source
#   rows from entering raw-file storage, audit lifecycle, or downstream skips.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     supplies the decoded Google response object.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     adsense_management.py -> must only receive complete ReportResult rows.
# ============================================================================
def _reject_truncated_report_result(
    *, response_json: dict[str, object], url: str,
) -> None:
    """Raise when the AdSense ReportResult row counts prove truncation."""
    rows = _result_rows(response_json=response_json, url=url)
    returned_count = len(rows) if rows is not None else 0
    total = _total_matched_rows(response_json=response_json, url=url)

    if returned_count > _MAX_REPORT_RESULT_ROWS:
        raise GoogleApiResponseError(
            url=url,
            reason=(
                "ReportResult returned more rows than the AdSense row cap "
                f"({returned_count}>{_MAX_REPORT_RESULT_ROWS})"
            ),
        )
    if total is not None and total < returned_count:
        raise GoogleApiResponseError(
            url=url,
            reason=(
                "totalMatchedRows is smaller than returned rows "
                f"({total}<{returned_count})"
            ),
        )
    if total is not None and total > returned_count:
        raise GoogleApiResponseError(
            url=url,
            reason=(
                "truncated ReportResult "
                f"(totalMatchedRows={total}, returnedRows={returned_count})"
            ),
        )
    if total is None and returned_count >= _MAX_REPORT_RESULT_ROWS:
        raise GoogleApiResponseError(
            url=url,
            reason=(
                "possible truncated ReportResult at AdSense row cap without "
                "totalMatchedRows"
            ),
        )


# ============================================================================
# Purpose: Wrap the raw reports.generate JSON body into the dict shape that
#   AdSenseManagementParser consumes, stamping a deterministic SHA-256
#   report_id derived from (account_id, report_month, locked-report-key).
#   AdSense reports.generate does not echo a stable report id, so this stamp
#   is the connector's source_report_id provenance for idempotent reruns.
# Database/ORM: None (pure response shaping; no DB or secret access).
# Standards: Typed dict[str, object] boundary; raises MalformedReportMonthError
#   for invalid date bounds and MalformedAdsenseAccountIdError for unsafe
#   account paths; missing rows tolerated as None (parser already maps None ->
#   empty list).
# Blast Radius: Connector parser-payload only — no finance/audit/Neo4j write
#   here. A drift in the stamped key would change provenance across reruns
#   and break source_report_id-based audit traceability for the same slice.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     adsense_management.py -> consumes report_id + request/headers/rows.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-
#     design.md §5.6 -> AdSense ingestion contract.
# ============================================================================
def adsense_response_to_parser_payload(
    *,
    response_json: dict[str, object],
    account_id: str,
    report_month: str,
) -> dict[str, object]:
    """Wrap a reports.generate body with a deterministic report_id stamp."""
    account_id = _validated_account_id(account_id)
    report_id = hashlib.sha256(
        f"{account_id}|{report_month}|{_REPORT_KEY}".encode()
    ).hexdigest()
    payload = dict(response_json)
    payload["request"] = _synthesized_request(
        account_id=account_id, report_month=report_month,
    )
    payload["headers"] = response_json.get("headers", [])
    # Parser tolerates a missing/None `rows` as a clean zero-result; passing
    # the raw .get() result preserves that contract without re-defaulting.
    payload["rows"] = response_json.get("rows")
    payload["report_id"] = report_id
    return payload


# ============================================================================
# Purpose: Thin wrapper around GoogleHttpClient for AdSense Management v2
#   reports.generate. Issues one HTTP GET per (account_id, report_month)
#   slice with the locked query envelope and returns a parser-ready payload.
# Database/ORM: None (HTTP client only; persistence is owned by orchestrator).
# Standards: Typed keyword-only parameters; delegates HTTP + retry policy
#   entirely to GoogleHttpClient.request(); raises GoogleConnectorError
#   subclasses (auth/rate-limit/server/client/response) on any non-200
#   outcome or schema gap, plus MalformedReportMonthError for bad input.
# Blast Radius: Connector ingestion of AdSense earnings evidence.
#   A drift in date bounds or the metric pair would mis-attribute or
#   silently drop revenue evidence. AdSense rows are skipped in C1 as
#   MISSING_CHANNEL_ID, so a bug here doesn't reach finance totals — but
#   it would still corrupt the audit evidence trail.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     GoogleHttpClient.request handles Bearer auth, retry, and JSON decode.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     adsense_management.py -> consumes the parser-ready payload shape.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-
#     design.md §5.6 -> endpoint, locked params, and parser contract.
# ============================================================================
class AdSenseManagementClient:
    """Thin wrapper around GoogleHttpClient for AdSense reports.generate."""

    def __init__(self, *, http: GoogleHttpClient) -> None:
        """Bind the shared HTTP client (auth + retry + JSON decode)."""
        self._http = http

    def fetch_monthly_report(
        self, *, account_id: str, report_month: str,
    ) -> dict[str, object]:
        """Fetch one AdSense account's monthly reports.generate JSON, wrapped
        with the deterministic report_id stamp the parser requires.
        """
        account_id = _validated_account_id(account_id)
        year_i, month_i, last_day = _report_month_date_range(report_month)
        url = f"{_BASE}/accounts/{account_id}/reports:generate"
        params = [
            ("dateRange", "CUSTOM"),
            ("startDate.year", str(year_i)),
            ("startDate.month", str(month_i)),
            ("startDate.day", "1"),
            ("endDate.year", str(year_i)),
            ("endDate.month", str(month_i)),
            ("endDate.day", str(last_day)),
            ("dimensions", _DIMENSIONS),
            *[("metrics", metric) for metric in _METRICS],
            ("currencyCode", _CURRENCY),
        ]
        response = self._http.request(method="GET", url=url, params=params)
        _reject_truncated_report_result(response_json=response, url=url)
        return adsense_response_to_parser_payload(
            response_json=response,
            account_id=account_id,
            report_month=report_month,
        )
