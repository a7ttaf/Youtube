"""YouTube Reporting v1 API client.

Endpoints used (Bearer auth via GoogleHttpClient):
- GET /v1/jobs                 -> list reporting jobs
- GET /v1/jobs/{jobId}/reports -> list reports under a job (date-filtered)
- GET <downloadUrl>            -> raw CSV bytes

Caller filters by SUPPORTED_REPORT_TYPES on list_supported_jobs and
date-bounds on list_reports_for_month.
"""
from __future__ import annotations

from datetime import date

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
            params: dict[str, str] = {"onBehalfOfContentOwner": account_id}
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            for job in body.get("jobs", []):
                if job.get("reportTypeId") in SUPPORTED_REPORT_TYPES:
                    out.append(job)
            token = body.get("nextPageToken")
            if not token:
                break
        return out

    # ============================================================================
    # Purpose: List the reports a job produced for a single calendar month,
    #          paginated. Returns Google's report descriptors (id, downloadUrl,
    #          startTime, etc.) for the orchestrator to fetch individually.
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
        self, *, account_id: str, job_id: str, report_month: str,
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
            out.extend(body.get("reports", []))
            token = body.get("nextPageToken")
            if not token:
                break
        return out
