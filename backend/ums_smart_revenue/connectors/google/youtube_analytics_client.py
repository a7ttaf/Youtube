"""YouTube Analytics v2 reports.query targeted CMS-channel ingestion (spec §5.5).

Endpoint: GET https://youtubeanalytics.googleapis.com/v2/reports
Query params:
  ids=contentOwner==<cms_account_id>
  filters=channel==<youtube_channel_id>
  startDate=<YYYY-MM-01>
  endDate=<YYYY-MM-01>
  metrics=estimatedRevenue,...   <- locked per parser requirements
  dimensions=month

The wire-level dimension set is intentionally ``month`` only: Google's
content-owner report contract requires the `channel` dimension to be paired
with a multi-value channel filter, while B2.5 issues one request per channel
with `filters=channel==<id>` (a single value). YouTubeAnalyticsParser still
keys rows on (channel, month); the orchestrator's YouTubeAnalyticsRunner
synthesises the `channel` dimension into the parser payload from the known
filter value before yielding the report.

Channels are sourced from the youtube_channels registry (PR #25) filtered
by tenant + active + revenue_required + content_owner match only. Outside-CMS
revenue sourcing remains unresolved and is not ingested here.
"""

from __future__ import annotations

import re
from calendar import monthrange
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    MalformedAnalyticsSelectorError,
    MalformedReportMonthError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.db.org_models import YouTubeChannelORM

_REPORT_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"
# Locked metric set; matches what YouTubeAnalyticsParser consumes.
_METRICS = "estimatedRevenue,estimatedAdRevenue,grossRevenue"
_COUNTRY_EVIDENCE_METRICS = "estimatedRevenue"
# Single-channel content-owner reports use the time dimension only (see module
# docstring). The orchestrator runner re-introduces the `channel` dimension into
# the parser payload from the known filter so YouTubeAnalyticsParser keeps its
# (channel, month) row-key contract without a parser change.
# FIX: Switch _DIMENSIONS from "channel,month" to "month" only. Google's
# content-owner reports require the `channel` dimension to be paired with a
# multi-value channel filter; B2.5 issues one request per channel (single-value
# filter), so adding `channel` to the wire dimensions can be rejected in live
# runs even though mocked tests accept it. The channel dimension is re-
# synthesized downstream from the known filter value by
# YouTubeAnalyticsRunner._synthesise_analytics_channel_dimension so the parser
# contract is preserved.
_DIMENSIONS = "month"
_COUNTRY_EVIDENCE_DIMENSIONS = "country"


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
    *,
    account_id: str,
    channel_id: str,
    report_month: str,
) -> dict[str, str]:
    """Return the canonical reports.query parameters for one channel-month slice."""
    # FIX: fail closed on empty/whitespace account_id or channel_id before
    # constructing the selector strings. Without this guard the request would
    # serialize to `ids=contentOwner==` / `filters=channel==`, which the
    # YouTube Analytics API rejects opaquely and which (if it ever persisted)
    # would write a source_account_id with no owner/channel identity. Both
    # values are operator/registry-controlled, so this is a typed boundary
    # check, not user input sanitisation.
    if not isinstance(account_id, str) or not account_id.strip():
        raise MalformedAnalyticsSelectorError(
            field_name="account_id",
            value=account_id,
        )
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise MalformedAnalyticsSelectorError(
            field_name="channel_id",
            value=channel_id,
        )
    account_id = account_id.strip()
    channel_id = channel_id.strip()
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
# Purpose: Build a country-dimensional Analytics request whose rows are stored
#          only as U2 provenance and are forbidden from finance projection.
# Database/ORM: None.
# Standards: Reuses the canonical account/channel/month validation boundary;
#            deterministic request parameters; no withholding calculation.
# Blast Radius: Google API volume and source-row evidence only. Official
#               finance facts, totals, exports, and reconciliation are fenced.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> Opt-in
#     runner lane persists the response under source_system youtube_analytics.
#   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py ->
#     Rejects these rows from canonical projection before bucketing.
# ============================================================================
def _build_country_evidence_query_request(
    *,
    account_id: str,
    channel_id: str,
    report_month: str,
) -> dict[str, str]:
    """Return one channel-month country evidence reports.query request."""
    base = _build_query_request(
        account_id=account_id,
        channel_id=channel_id,
        report_month=report_month,
    )
    return {
        **base,
        "endDate": calendar_month_end_iso(report_month),
        "metrics": _COUNTRY_EVIDENCE_METRICS,
        "dimensions": _COUNTRY_EVIDENCE_DIMENSIONS,
    }


# ============================================================================
# Purpose: Return the calendar-month-end ISO date for a YYYY-MM report_month.
#   The wire request constrains startDate/endDate to the first-of-month (Google
#   requires both ends to be the first day when `dimensions=month`), but the
#   parser persists `endDate` as each source row's period_end. Without an
#   override the row would record period_end = first-of-month for what is a
#   whole-month aggregate. The orchestrator runner uses this helper to stamp
#   the parser payload's `query_request.endDate` with the actual coverage end
#   so persisted source rows record the correct period range.
# Database/ORM: None.
# Standards: Validates report_month identically to `_build_query_request`;
#   raises MalformedReportMonthError on malformed input.
# Blast Radius: Source-of-truth `period_end` on persisted analytics rows. A
#   drift here would mis-record the coverage window for downstream auditing
#   and revenue-fact normalisation.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     YouTubeAnalyticsRunner overrides parser-payload `endDate` with this
#     return value while keeping the wire request first-of-month.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> parser persists `endDate` as period_end.
# ============================================================================
def calendar_month_end_iso(report_month: str) -> str:
    """Return ``YYYY-MM-DD`` for the last day of the report_month."""
    if not _REPORT_MONTH_PATTERN.fullmatch(report_month):
        raise MalformedReportMonthError(report_month=report_month)
    year_s, month_s = report_month.split("-")
    year, month = int(year_s), int(month_s)
    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


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
    session: Session,
    *,
    tenant_id: UUID,
    account_id: str,
) -> list[str]:
    """Return eligible CMS-owned channel IDs for the tenant/account, sorted asc.

    Eligibility requires ``cms_status='INSIDE_CMS'`` in addition to the
    content-owner match: an operator can manually flag a channel as
    ``OUTSIDE_CMS`` for tracking/manual-import workflows even while a
    ``content_owner_id`` is recorded (see ``backend/ums_smart_revenue/org/
    channel_issues.py``). B2.5 only ingests INSIDE_CMS revenue here, so those
    OUTSIDE_CMS rows must be filtered out at the registry boundary instead of
    silently leaking into the Analytics target set.
    """
    stmt = (
        select(YouTubeChannelORM.youtube_channel_id)
        .where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.revenue_required.is_(True),
            YouTubeChannelORM.content_owner_id == account_id,
            # FIX: exclude channels manually tagged OUTSIDE_CMS even if they
            # still carry a content_owner_id (tracking/manual-import case).
            # Without this guard the Analytics run would fetch and persist CMS
            # rows for a channel the operator has explicitly removed from the
            # B2.5 ingestion scope.
            YouTubeChannelORM.cms_status == "INSIDE_CMS",
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
        self,
        *,
        account_id: str,
        channel_id: str,
        report_month: str,
    ) -> dict[str, object]:
        """Fetch one CMS-owned channel's monthly reports.query JSON body."""
        params = _build_query_request(
            account_id=account_id,
            channel_id=channel_id,
            report_month=report_month,
        )
        return self._http.request(method="GET", url=_BASE, params=params)

    # ========================================================================
    # Purpose: Fetch the full country breakdown for one CMS channel-month as
    #          evidence-only U2 input.
    # Database/ORM: None.
    # Standards: Same authenticated/retrying HTTP boundary as monthly fetch;
    #            typed selector/month errors; no local finance math.
    # Blast Radius: Read-only Google API request. Downstream persistence is
    #               explicitly NON_PROJECTING_EVIDENCE.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
    #     YouTubeAnalyticsRunner invokes this only behind the opt-in gate.
    # ========================================================================
    def fetch_channel_country_evidence(
        self,
        *,
        account_id: str,
        channel_id: str,
        report_month: str,
    ) -> dict[str, object]:
        """Fetch one channel's country-dimensional revenue evidence."""
        params = _build_country_evidence_query_request(
            account_id=account_id,
            channel_id=channel_id,
            report_month=report_month,
        )
        return self._http.request(method="GET", url=_BASE, params=params)
