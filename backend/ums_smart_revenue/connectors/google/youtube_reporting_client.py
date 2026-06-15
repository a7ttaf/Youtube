"""YouTube Reporting v1 API client.

Endpoints used (Bearer auth via GoogleHttpClient):
- GET /v1/jobs                 -> list reporting jobs
- GET /v1/jobs/{jobId}/reports -> list reports under a job (date-filtered)
- GET <downloadUrl>            -> raw CSV bytes

Caller filters by SUPPORTED_REPORT_TYPES on list_supported_jobs and
date-bounds on list_reports_for_month.
"""

from __future__ import annotations

import ipaddress
from datetime import date
from urllib.parse import urlparse

from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.report_type_whitelist import (
    SUPPORTED_REPORT_TYPES,
)

_BASE = "https://youtubereporting.googleapis.com/v1"


def _month_bounds_iso(report_month: str) -> tuple[str, str]:
    year, month = report_month.split("-")
    year_i, month_i = int(year), int(month)
    start = date(year_i, month_i, 1)
    if month_i == 12:
        end = date(year_i + 1, 1, 1)
    else:
        end = date(year_i, month_i + 1, 1)
    return (
        f"{start.isoformat()}T00:00:00Z",
        f"{end.isoformat()}T00:00:00Z",
    )


def _text_sort_key(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _report_freshness_key(report: dict[str, object]) -> tuple[str, str, str]:
    return (
        _text_sort_key(report.get("createTime")),
        _text_sort_key(report.get("startTime")),
        _text_sort_key(report.get("id")),
    )


# ============================================================================
# Purpose: Group replacement YouTube Reporting descriptors by the period they
#          cover so only the newest descriptor for a day/month is downloaded.
# Database/ORM: None.
# Standards: Pure descriptor normalization; no network, database, or secret
#            access. Missing period fields fall back to report id.
# Blast Radius: Connector download selection only. Authorization, finance,
#               audit, Neo4j, and exports are unaffected until orchestration.
# Connections:
#   - Function: _newest_reports_by_period -> uses this key for replacement
#     report de-duplication.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     consumes the sorted report descriptors returned by the client.
# ============================================================================
def _report_period_key(report: dict[str, object]) -> tuple[str, str, str]:
    start = _text_sort_key(report.get("startTime"))
    end = _text_sort_key(report.get("endTime"))
    if start or end:
        return ("period", start, end)
    return ("report", _text_sort_key(report.get("id")), "")


def _newest_reports_by_period(reports: list[dict[str, object]]) -> list[dict[str, object]]:
    latest: dict[tuple[str, str, str], dict[str, object]] = {}
    for report in reports:
        period_key = _report_period_key(report)
        current = latest.get(period_key)
        if current is None or _report_freshness_key(report) > _report_freshness_key(current):
            latest[period_key] = report
    return sorted(
        latest.values(),
        key=lambda report: (
            _text_sort_key(report.get("startTime")),
            _text_sort_key(report.get("endTime")),
            _report_freshness_key(report),
        ),
    )


# ============================================================================
# Purpose: Validate Google list-envelope fields before callers iterate or sort
#          descriptor objects.
# Database/ORM: None.
# Standards: Missing list fields mean an empty page; malformed/null/non-object
#            list payloads raise typed GoogleApiResponseError.
# Blast Radius: API response-shape validation only. Authorization, finance,
#               audit, Neo4j, and exports are unaffected until descriptors are
#               accepted by the orchestrator.
# Connections:
#   - Method: YouTubeReportingClient.list_supported_jobs -> validates jobs.
#   - Method: YouTubeReportingClient.list_reports_for_month -> validates reports.
# ============================================================================
def _response_object_list(
    body: dict[str, object], field: str, *, url: str
) -> list[dict[str, object]]:
    if field not in body:
        return []
    value = body[field]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GoogleApiResponseError(url=url, reason=f"{field!r} must be a list of objects")
    return value


# ============================================================================
# Purpose: Validate Google's pagination token before it is reused as pageToken.
# Database/ORM: None.
# Standards: Missing or empty tokens terminate pagination; malformed tokens
#            raise typed GoogleApiResponseError at the API boundary.
# Blast Radius: Connector API pagination only. Authorization, finance, audit,
#               Neo4j, and exports are unaffected until descriptors are accepted.
# Connections:
#   - Method: YouTubeReportingClient.list_supported_jobs -> validates job pages.
#   - Method: YouTubeReportingClient.list_reports_for_month -> validates report pages.
# ============================================================================
def _next_page_token(body: dict[str, object], *, url: str) -> str | None:
    if "nextPageToken" not in body:
        return None
    value = body["nextPageToken"]
    if value == "":
        return None
    # FIX: Distinguish terminal absence/empty-string from malformed non-string
    # tokens so pagination cannot silently stop or reuse an object as pageToken.
    if not isinstance(value, str) or not value.strip():
        raise GoogleApiResponseError(url=url, reason="'nextPageToken' must be a non-empty string")
    return value


class YouTubeReportingClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    # ============================================================================
    # Purpose: List the YouTube Reporting jobs for an account, filtered to the
    #          locked-at-ship report_type_id whitelist (B1 parser-compatible set).
    # Database/ORM: None (read-only API call).
    # Standards: Pagination via nextPageToken; typed errors via GoogleHttpClient
    #            (4xx/auth/429/5xx/response). Out-of-whitelist jobs are dropped
    #            here so the orchestrator never sees an UnsupportedReportTypeError
    #            for known-bad report types.
    # Blast Radius: Whitelist filter is the load-bearing guard between Google's
    #               jobs catalog and the B1 parser; widening it requires both a
    #               parser change and a SUPPORTED_REPORT_TYPES update.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     typed retry/error/response-validation pipeline.
    #   - File: backend/ums_smart_revenue/connectors/google/report_type_whitelist.py ->
    #     SUPPORTED_REPORT_TYPES source of truth.
    #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
    #     §5.4 -> supported report types and orchestrator integration contract.
    # ============================================================================
    def list_supported_jobs(self, *, account_id: str) -> list[dict[str, object]]:
        url = f"{_BASE}/jobs"
        token: str | None = None
        out: list[dict[str, object]] = []
        while True:
            params: dict[str, str] = {
                "onBehalfOfContentOwner": account_id,
                "includeSystemManaged": "true",
            }
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            for job in _response_object_list(body, "jobs", url=url):
                if job.get("reportTypeId") in SUPPORTED_REPORT_TYPES:
                    out.append(job)
            token = _next_page_token(body, url=url)
            if not token:
                break
        return out

    # ============================================================================
    # Purpose: List the newest reports a job produced for each period in a
    #          single calendar month, paginated. Returns Google's report
    #          descriptors (id, downloadUrl, startTime, etc.) for the
    #          orchestrator to fetch individually, sorted by report period.
    # Database/ORM: None (read-only API call).
    # Standards: Month bounds form a half-open interval [start, next-month-start)
    #            via _month_bounds_iso (RFC 3339 Z-suffix). Pagination via
    #            nextPageToken with date bounds re-sent on every page so a
    #            future refactor can't accidentally drop them after page 1.
    # Blast Radius: Wrong month-bound math would miss reports (revenue gaps) or
    #               double-count across boundary months (revenue inflation).
    #               December rollover is explicit; see _month_bounds_iso.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     typed retry/error/response-validation pipeline.
    #   - Helper: _month_bounds_iso (module scope above the class) -> month-to-ISO
    #     window with December branch.
    #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
    #     §5.4 -> orchestrator integration contract for per-month ingestion.
    # ============================================================================
    def list_reports_for_month(
        self,
        *,
        account_id: str,
        job_id: str,
        report_month: str,
    ) -> list[dict[str, object]]:
        url = f"{_BASE}/jobs/{job_id}/reports"
        start_iso, end_iso = _month_bounds_iso(report_month)
        token: str | None = None
        out: list[dict[str, object]] = []
        while True:
            params: dict[str, str] = {
                "onBehalfOfContentOwner": account_id,
                "startTimeAtOrAfter": start_iso,
                "startTimeBefore": end_iso,
            }
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            out.extend(_response_object_list(body, "reports", url=url))
            token = _next_page_token(body, url=url)
            if not token:
                break
        return _newest_reports_by_period(out)

    # ============================================================================
    # Purpose: Download the raw CSV bytes for one YouTube Reporting report,
    #          given the downloadUrl returned by list_reports_for_month. Bytes
    #          are handed straight to the orchestrator for blob upload and the
    #          B1 parser, with no in-memory decoding or shape validation here.
    # Database/ORM: None (read-only API download).
    # Standards: Delegates to GoogleHttpClient.fetch_bytes so the full retry +
    #            typed-error taxonomy (auth/client/rate-limit/server) applies
    #            identically to JSON and binary downloads. Returns raw bytes —
    #            no decoding, no validation, no header inference.
    # Blast Radius: This is the only path that pulls report payloads into the
    #               system. Adding decoding here would invert the parser's
    #               source-of-truth on encoding and break checksum hashes
    #               recorded against the on-disk blob.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     fetch_bytes (Bearer auth + spec §7 retry + typed errors).
    #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
    #     §5.4 -> orchestrator integration contract (download -> blob -> parse).
    # ============================================================================
    def fetch_report(self, *, download_url: str) -> bytes:
        _validate_google_download_url(download_url)
        return self._http.fetch_bytes(url=download_url)


# ============================================================================
# Purpose: Fail closed before fetching Google-provided report download URLs.
# Database/ORM: None.
# Standards: HTTPS-only Google domains; typed GoogleApiResponseError on drift.
# Blast Radius: Connector egress only. Authorization, finance, audit, Neo4j,
#               and exports are unaffected until bytes enter the orchestrator.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     fetch_bytes performs the authenticated HTTP request after validation.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.4 -> downloadUrl contract.
# ============================================================================
def _validate_google_download_url(download_url: str) -> None:
    parsed = urlparse(download_url)
    if parsed.scheme != "https":
        raise GoogleApiResponseError(url="<downloadUrl>", reason="downloadUrl must use https")
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        raise GoogleApiResponseError(url="<downloadUrl>", reason="downloadUrl host is missing")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise GoogleApiResponseError(
            url="<downloadUrl>", reason="downloadUrl host must be a Google domain"
        )
    if host == "localhost" or host.endswith(".localhost"):
        raise GoogleApiResponseError(
            url="<downloadUrl>", reason="downloadUrl host must be a Google domain"
        )
    allowed_suffixes = ("googleapis.com", "googleusercontent.com")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes):
        raise GoogleApiResponseError(
            url="<downloadUrl>", reason="downloadUrl host must be a Google domain"
        )
