"""YouTube Analytics v2 reports.query targeted channel ingestion (spec §5.5).

Endpoint: GET https://youtubeanalytics.googleapis.com/v2/reports
Query params:
  ids=channel==<youtube_channel_id>
  startDate=<YYYY-MM-01>
  endDate=<YYYY-MM-last>
  metrics=estimatedRevenue,...   <- locked per parser requirements
  dimensions=month,...

Channels are sourced from the youtube_channels registry (PR #25) filtered
by tenant + active + revenue_required + content_owner match-or-null.
"""
from __future__ import annotations

import re
from calendar import monthrange
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
_DIMENSIONS = "month"


# ============================================================================
# Purpose: Query the youtube_channels registry for channels that belong to
#   a tenant and are eligible for revenue ingestion from a given CMS account.
#   A channel is eligible when active=True, revenue_required=True, and its
#   content_owner_id either matches the account_id or is NULL (outside-CMS
#   channels are always included for the tenant regardless of account).
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
    stmt = (
        select(YouTubeChannelORM.youtube_channel_id)
        .where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.revenue_required.is_(True),
            (
                (YouTubeChannelORM.content_owner_id == account_id)
                | (YouTubeChannelORM.content_owner_id.is_(None))
            ),
        )
        .order_by(YouTubeChannelORM.youtube_channel_id.asc())
    )
    return [row[0] for row in session.execute(stmt).all()]


class YouTubeAnalyticsClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    # ============================================================================
    # Purpose: Issue a single YouTube Analytics v2 reports.query GET request for
    #   one channel and one calendar month, returning the parsed JSON body ready
    #   for YouTubeAnalyticsParser. Date bounds are computed from report_month so
    #   the caller never constructs raw date strings.
    # Database/ORM: None.
    # Standards: Typed keyword-only parameters; delegates HTTP + retry policy
    #   entirely to GoogleHttpClient.request(); raises GoogleConnectorError
    #   subclasses (auth/rate-limit/server/client) on any non-200 outcome.
    # Blast Radius: Finance revenue ingestion — each call produces the raw
    #   payload for one channel-month. A bug here (wrong date bounds, wrong
    #   channel id format) would produce incorrect or empty revenue rows without
    #   raising an error. The ids= format "channel==<id>" is required by the
    #   YouTube Analytics API and must not be altered.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     GoogleHttpClient.request() handles Bearer auth, retry, and JSON decode.
    #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-
    #     design.md §5.5 -> endpoint, metric set, and dimension contract.
    # ============================================================================
    def fetch_channel_report(
        self, *, channel_id: str, report_month: str,
    ) -> dict[str, object]:
        if not _REPORT_MONTH_PATTERN.fullmatch(report_month):
            raise MalformedReportMonthError(report_month=report_month)
        year, month = report_month.split("-")
        year_i, month_i = int(year), int(month)
        last_day = monthrange(year_i, month_i)[1]
        params = {
            "ids": f"channel=={channel_id}",
            "startDate": f"{year}-{month}-01",
            "endDate": f"{year}-{month}-{last_day:02d}",
            "metrics": _METRICS,
            "dimensions": _DIMENSIONS,
        }
        return self._http.request(method="GET", url=_BASE, params=params)
