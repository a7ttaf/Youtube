from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError, SqlAlchemyRevenueFactRepository


USER_ID = "00000000-0000-0000-0000-000000010001"
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000010002")


def test_revenue_fact_repository_rejects_invalid_metric_ranges():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=CHANNEL_ROW_ID,
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            )
        )
        session.commit()

        repository = SqlAlchemyRevenueFactRepository(session)
        with pytest.raises(RevenueFactValidationError, match="views must be >= 0"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=-1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="watch_time_minutes must be >= 0"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("-0.01"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="confidence_score must be between 0 and 1"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("1.0001"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="confidence_score must be between 0 and 1"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("-0.0001"),
                actor_user_id=USER_ID,
            )


def test_revenue_fact_repository_rejects_invalid_amounts_and_non_finite_metrics():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=CHANNEL_ROW_ID,
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            )
        )
        session.commit()

        repository = SqlAlchemyRevenueFactRepository(session)
        with pytest.raises(RevenueFactValidationError, match="gross_revenue_usd must be a finite decimal >= 0"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("-0.01"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="net_revenue_usd must be a finite decimal >= 0"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=Decimal("NaN"),
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="watch_time_minutes must be a finite decimal"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("Infinity"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        with pytest.raises(RevenueFactValidationError, match="confidence_score must be a finite decimal"):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("NaN"),
                actor_user_id=USER_ID,
            )
