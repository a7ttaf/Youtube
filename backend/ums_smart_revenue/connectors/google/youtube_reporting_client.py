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
