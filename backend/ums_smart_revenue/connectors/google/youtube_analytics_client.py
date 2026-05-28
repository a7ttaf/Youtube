"""YouTube Analytics v2 reports.query targeted CMS-channel ingestion (spec §5.5).

Endpoint: GET https://youtubeanalytics.googleapis.com/v2/reports
Query params:
  ids=contentOwner==<cms_account_id>
  filters=channel==<youtube_channel_id>
  startDate=<YYYY-MM-01>
  endDate=<YYYY-MM-01>
  metrics=estimatedRevenue,...   <- locked per parser requirements
  dimensions=channel,month

Channels are sourced from the youtube_channels registry (PR #25) filtered
by tenant + active + revenue_required + content_owner match only. Outside-CMS
revenue sourcing remains unresolved and is not ingested here.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import MalformedReportMonthError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.db.org_models import YouTubeChannelORM

_REPORT_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"
# Locked metric set; matches what YouTubeAnalyticsParser consumes.
_METRICS = "estimatedRevenue,estimatedAdRevenue,grossRevenue"
_DIMENSIONS = "channel,month"


# ============================================================================
# Purpose: Build the canonical reports.query parameter set for one CMS-owned
#   channel-month slice. This keeps the HTTP request and the stored
#   query_request metadata identical so stale-row cleanup and replay use the
#   exact same account/channel scope.
# Database/ORM: None.
# Standards: Validates report_month once; returns deterministic str params; no
#   network side effects.
# Blast Radius: Finance ingestion scope and replay fidelity for YouTube
#   Analytics payloads. A drift here would mis-attribute source rows or fetch
#   an unsupported Google report shape.
# Connections:
#   - Method: YouTubeAnalyticsClient.fetch_channel_report -> sends these params
#     to Google.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     persists the same query_request alongside the response payload.
# ============================================================================
def _build_query_request(
    *, account_id: str, channel_id: str, report_month: str,
) -> dict[str, str]:
    """Return the canonical reports.query parameters for one channel-month slice."""
    if not _REPORT_MONTH_PATTERN.fullmatch(report_month):
        raise MalformedReportMonthError(report_month=report_month)
    year, month = report_month.split("-")
    first_day = f"{year}-{month}-01"
    return {
        "ids": f"contentOwner=={account_id}",
        "filters": f"channel=={channel_id}",
        "startDate": first_day,
        "endDate": first_day,
        "metrics": _METRICS,
        "dimensions": _DIMENSIONS,
    }


# ============================================================================
# Purpose: Query the youtube_channels registry for channels that belong to
#   a tenant and are eligible for revenue ingestion from a given CMS account.
#   A channel is eligible when active=True, revenue_required=True, and its
#   content_owner_id matches the account_id. Outside-CMS channels are excluded
#   until their revenue source is implemented separately.
# Database/ORM: YouTubeChannelORM (youtube_channels table).
# Standards: Typed UUID boundary; parameterized SQLAlchemy select; ordering
#   is deterministic (ascending youtube_channel_id) for stable test assertions
#   and reproducible ingestion runs.
# Blast Radius: Finance ingestion scope — only channels returned here will
#   have reports fetched and parsed. Incorrect filtering could silently omit
#   revenue channels or include channels for a different account. No write.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> YouTubeChannelORM
#     fields active, revenue_required, content_owner_id, tenant_id.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> YouTubeAnalyticsClient.fetch_channel_report uses this list to drive
#     per-channel report fetches.
# ============================================================================
def list_target_channels(
    session: Session, *, tenant_id: UUID, account_id: str,
) -> list[str]:
    """Return eligible CMS-owned channel IDs for the tenant/account, sorted asc."""
    stmt = (
        select(YouTubeChannelORM.youtube_channel_id)
        .where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.revenue_required.is_(True),
            YouTubeChannelORM.content_owner_id == account_id,
        )
        .order_by(YouTubeChannelORM.youtube_channel_id.asc())
    )
    return [row[0] for row in session.execute(stmt).all()]


class YouTubeAnalyticsClient:
    """Thin wrapper around GoogleHttpClient for YouTube Analytics reports.query."""

    def __init__(self, *, http: GoogleHttpClient) -> None:
        """Bind the shared HTTP client (auth + retry + JSON decode)."""
        self._http = http

    # ============================================================================
    # Purpose: Issue a single YouTube Analytics v2 reports.query GET request for
    #   one CMS-owned channel and one calendar month, returning the parsed JSON
    #   body ready for YouTubeAnalyticsParser. The request is content-owner
    #   scoped and channel-filtered so revenue metrics remain on a supported
    #   Google contract while preserving per-channel ingestion.
    # Database/ORM: None.
    # Standards: Typed keyword-only parameters; delegates HTTP + retry policy
    #   entirely to GoogleHttpClient.request(); raises GoogleConnectorError
    #   subclasses (auth/rate-limit/server/client) on any non-200 outcome.
    # Blast Radius: Finance revenue ingestion — each call produces the raw
    #   payload for one channel-month. A bug here (wrong date bounds, wrong
    #   account/channel scoping) would produce incorrect or empty revenue rows
    #   without raising an error.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     GoogleHttpClient.request() handles Bearer auth, retry, and JSON decode.
    #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-
    #     design.md §5.5 -> endpoint, metric set, and dimension contract.
    # ============================================================================
    def fetch_channel_report(
        self, *, account_id: str, channel_id: str, report_month: str,
    ) -> dict[str, object]:
        """Fetch one CMS-owned channel's monthly reports.query JSON body."""
        params = _build_query_request(
            account_id=account_id,
            channel_id=channel_id,
            report_month=report_month,
        )
        return self._http.request(method="GET", url=_BASE, params=params)
