"""YouTube Analytics targeted channel ingestion tests (spec §5.5)."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    MalformedAnalyticsSelectorError,
    MalformedReportMonthError,
)
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
    cms_status: str = "INSIDE_CMS",
) -> YouTubeChannelORM:
    """Build one YouTubeChannelORM row for target-channel selection tests.

    ``cms_status`` defaults to ``INSIDE_CMS`` so a bare-options channel is
    eligible for the analytics target set; pass ``"OUTSIDE_CMS"`` or
    ``"UNKNOWN"`` to drive the exclusion cases.
    """
    return YouTubeChannelORM(
        id=uuid4(),
        tenant_id=tenant_id,
        youtube_channel_id=youtube_channel_id,
        channel_name=youtube_channel_id,
        content_owner_id=content_owner_id,
        active=active,
        revenue_required=revenue_required,
        cms_status=cms_status,
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


def test_list_target_channels_excludes_outside_cms_tagged_channels(
    db_session: Session,
) -> None:
    """Channels with a content_owner_id but ``cms_status='OUTSIDE_CMS'`` stay out.

    Covers the codex P2 finding: an operator can manually flag a channel as
    OUTSIDE_CMS while it still carries a content_owner_id (tracking /
    manual-import workflows in `channel_issues.py`). The Analytics ingestion
    contract is INSIDE_CMS-only, so this row must be filtered out.
    """
    tenant_id = uuid4()
    db_session.add_all(
        [
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-inside",
                content_owner_id="o",
                cms_status="INSIDE_CMS",
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-outside-tagged",
                content_owner_id="o",
                cms_status="OUTSIDE_CMS",
            ),
            _build_channel(
                tenant_id=tenant_id,
                youtube_channel_id="UC-unknown",
                content_owner_id="o",
                cms_status="UNKNOWN",
            ),
        ]
    )
    db_session.flush()
    channels = list_target_channels(db_session, tenant_id=tenant_id, account_id="o")
    assert channels == ["UC-inside"]


def test_fetch_channel_report(mock_credentials) -> None:
    """The client must send content-owner IDs, per-channel filter, and month-only dimension.

    Single-channel content-owner reports do not accept `channel` as a dimension
    (the dimension is reserved for multi-value channel filters), so the wire
    request uses `dimensions=month`. The orchestrator runner re-injects the
    `channel` dimension into the parser payload from the known filter, keeping
    YouTubeAnalyticsParser's (channel, month) row-key contract intact.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Assert the wire request shape for one CMS-owned channel-month slice."""
        params = dict(request.url.params)
        assert params["ids"] == "contentOwner==owner-a"
        assert params["filters"] == "channel==UC-xyz"
        assert params["startDate"] == "2026-05-01"
        assert params["endDate"] == "2026-05-01"
        assert params["metrics"] == "estimatedRevenue,estimatedAdRevenue,grossRevenue"
        assert params["dimensions"] == "month"
        body = json.dumps(
            {"rows": [["2026-05", 1.23, 0.45, 1.68]]}
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
    assert body == {"rows": [["2026-05", 1.23, 0.45, 1.68]]}


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


@pytest.mark.parametrize("bad_account", ["", "   ", "\t\n"])
def test_fetch_channel_report_rejects_empty_account_id(
    mock_credentials, bad_account: str,
) -> None:
    """Empty/whitespace account_id must fail closed before any HTTP traffic."""
    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    client = YouTubeAnalyticsClient(http=http)
    with pytest.raises(MalformedAnalyticsSelectorError) as exc_info:
        client.fetch_channel_report(
            account_id=bad_account,
            channel_id="UC-xyz",
            report_month="2026-05",
        )
    assert exc_info.value.field_name == "account_id"


@pytest.mark.parametrize("bad_channel", ["", "   ", "\t\n"])
def test_fetch_channel_report_rejects_empty_channel_id(
    mock_credentials, bad_channel: str,
) -> None:
    """Empty/whitespace channel_id must fail closed before any HTTP traffic."""
    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    client = YouTubeAnalyticsClient(http=http)
    with pytest.raises(MalformedAnalyticsSelectorError) as exc_info:
        client.fetch_channel_report(
            account_id="owner-a",
            channel_id=bad_channel,
            report_month="2026-05",
        )
    assert exc_info.value.field_name == "channel_id"
