"""YouTube Analytics targeted channel ingestion tests (spec §5.5)."""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import MalformedReportMonthError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    YouTubeAnalyticsClient,
    list_target_channels,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    OrgBase.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


def _insert_channel(session, *, tenant_id, youtube_channel_id, content_owner_id, active=True, revenue_required=True):
    ch = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=tenant_id,
        youtube_channel_id=youtube_channel_id,
        channel_name=youtube_channel_id,
        content_owner_id=content_owner_id,
        active=active,
        revenue_required=revenue_required,
    )
    session.add(ch)
    session.flush()


def test_list_target_channels_includes_cms_match_and_outside_cms(session) -> None:
    tenant_id = uuid4()
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-1", content_owner_id="owner-a")
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-2", content_owner_id=None)
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-3", content_owner_id="owner-b")
    channels = list_target_channels(session, tenant_id=tenant_id, account_id="owner-a")
    assert channels == ["UC-1", "UC-2"]


def test_list_target_channels_excludes_inactive_and_no_revenue(session) -> None:
    tenant_id = uuid4()
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-1", content_owner_id="o", active=False)
    _insert_channel(
        session, tenant_id=tenant_id, youtube_channel_id="UC-2",
        content_owner_id="o", revenue_required=False,
    )
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-3", content_owner_id="o")
    channels = list_target_channels(session, tenant_id=tenant_id, account_id="o")
    assert channels == ["UC-3"]


def test_fetch_channel_report(mock_credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["ids"] == "channel==UC-xyz"
        assert params["startDate"] == "2026-05-01"
        assert params["endDate"] == "2026-05-31"
        assert params["metrics"] == "estimatedRevenue,estimatedAdRevenue,grossRevenue"
        assert params["dimensions"] == "channel,month"
        return httpx.Response(200, content=json.dumps({"rows": [["2026-05", "USD", "1.23"]]}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeAnalyticsClient(http=http)
    body = client.fetch_channel_report(channel_id="UC-xyz", report_month="2026-05")
    assert body == {"rows": [["2026-05", "USD", "1.23"]]}


@pytest.mark.parametrize("bad_month", ["2026-5", "2026", "abcd-ef", "2026-13"])
def test_fetch_channel_report_rejects_malformed_report_month(mock_credentials, bad_month: str) -> None:
    http = GoogleHttpClient(credentials=mock_credentials, transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    client = YouTubeAnalyticsClient(http=http)
    with pytest.raises(MalformedReportMonthError):
        client.fetch_channel_report(channel_id="UC-xyz", report_month=bad_month)
