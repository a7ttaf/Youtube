"""YouTube Analytics targeted channel ingestion tests (spec §5.5)."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

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


@pytest.fixture(name="db_session")
def _db_session_fixture() -> Session:
    """Create an isolated channel registry for analytics target selection."""
    eng = create_engine("sqlite:///:memory:")
    OrgBase.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


def _build_channel(
    *,
    tenant_id: UUID,
    youtube_channel_id: str,
    content_owner_id: str | None,
    active: bool = True,
    revenue_required: bool = True,
) -> YouTubeChannelORM:
    """Build one YouTubeChannelORM row for target-channel selection tests."""
    return YouTubeChannelORM(
        id=uuid4(),
        tenant_id=tenant_id,
        youtube_channel_id=youtube_channel_id,
        channel_name=youtube_channel_id,
        content_owner_id=content_owner_id,
        active=active,
        revenue_required=revenue_required,
    )


def test_list_target_channels_excludes_outside_cms_channels(
    db_session: Session,
) -> None:
    """Only CMS-owned channels for the matching tenant/account are eligible."""
    tenant_id = uuid4()
    db_session.add_all(
        [
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-1",
                content_owner_id="owner-a",
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-2",
                content_owner_id=None,
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-3",
                content_owner_id="owner-b",
            ),
            _build_channel(
                tenant_id=uuid4(),
                youtube_channel_id="UC-4",
                content_owner_id="owner-a",
            ),
        ]
    )
    db_session.flush()
    channels = list_target_channels(
        db_session,
        tenant_id=tenant_id,
        account_id="owner-a",
    )
    assert channels == ["UC-1"]


def test_list_target_channels_excludes_inactive_and_no_revenue(
    db_session: Session,
) -> None:
    """Inactive or non-revenue channels stay out of the analytics target set."""
    tenant_id = uuid4()
    db_session.add_all(
        [
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-1",
                content_owner_id="o",
                active=False,
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-2",
                content_owner_id="o",
                revenue_required=False,
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-3",
                content_owner_id="o",
            ),
        ]
    )
    db_session.flush()
    channels = list_target_channels(db_session, tenant_id=tenant_id, account_id="o")
    assert channels == ["UC-3"]


def test_fetch_channel_report(mock_credentials) -> None:
    """The client must send content-owner IDs and per-channel filters."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["ids"] == "contentOwner==owner-a"
        assert params["filters"] == "channel==UC-xyz"
        assert params["startDate"] == "2026-05-01"
        assert params["endDate"] == "2026-05-01"
        assert params["metrics"] == "estimatedRevenue,estimatedAdRevenue,grossRevenue"
        assert params["dimensions"] == "channel,month"
        body = json.dumps(
            {"rows": [["UC-xyz", "2026-05", 1.23, 0.45, 1.68]]}
        ).encode()
        return httpx.Response(200, content=body)

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeAnalyticsClient(http=http)
    body = client.fetch_channel_report(
        account_id="owner-a",
        channel_id="UC-xyz",
        report_month="2026-05",
    )
    assert body == {"rows": [["UC-xyz", "2026-05", 1.23, 0.45, 1.68]]}


@pytest.mark.parametrize("bad_month", ["2026-5", "2026", "abcd-ef", "2026-13"])
def test_fetch_channel_report_rejects_malformed_report_month(
    mock_credentials,
    bad_month: str,
) -> None:
    """Malformed month selectors must fail before the HTTP call matters."""
    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    client = YouTubeAnalyticsClient(http=http)
    with pytest.raises(MalformedReportMonthError):
        client.fetch_channel_report(
            account_id="owner-a",
            channel_id="UC-xyz",
            report_month=bad_month,
        )
