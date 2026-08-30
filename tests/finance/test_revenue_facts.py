from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactLockedMonthError,
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

USER_ID = "00000000-0000-0000-0000-000000010001"
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000010002")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000010999")
DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)


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

        with pytest.raises(
            RevenueFactValidationError,
            match="watch_time_minutes must be >= 0",
        ):
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

        with pytest.raises(
            RevenueFactValidationError,
            match="confidence_score must be between 0 and 1",
        ):
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

        with pytest.raises(
            RevenueFactValidationError,
            match="confidence_score must be between 0 and 1",
        ):
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
        with pytest.raises(
            RevenueFactValidationError,
            match="gross_revenue_usd must be a finite decimal >= 0",
        ):
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

        with pytest.raises(
            RevenueFactValidationError,
            match="net_revenue_usd must be a finite decimal >= 0",
        ):
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

        with pytest.raises(
            RevenueFactValidationError,
            match="watch_time_minutes must be a finite decimal",
        ):
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

        with pytest.raises(
            RevenueFactValidationError,
            match="confidence_score must be a finite decimal",
        ):
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


def test_revenue_fact_repository_upserts_duplicate_source_key_without_duplicate_rows():
    """A repeated month/channel/source input updates one canonical fact row."""
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
        repository.record_fact(
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            source_kind="YOUTUBE_CMS",
            source_report_id=None,
            gross_revenue_usd=Decimal("1000.00"),
            net_revenue_usd=None,
            views=1,
            watch_time_minutes=Decimal("1"),
            confidence_score=Decimal("0.9000"),
            actor_user_id=USER_ID,
        )
        repository.record_fact(
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            source_kind="YOUTUBE_CMS",
            source_report_id="cms-report-2026-03",
            gross_revenue_usd=Decimal("1100.00"),
            net_revenue_usd=None,
            views=2,
            watch_time_minutes=Decimal("2"),
            confidence_score=Decimal("0.9500"),
            actor_user_id=USER_ID,
        )
        session.commit()

        rows = session.scalars(select(MonthlyChannelRevenueFactORM)).all()
        assert len(rows) == 1
        assert rows[0].source_report_id == "cms-report-2026-03"
        assert rows[0].gross_revenue_usd == Decimal("1100.00")
        assert rows[0].views == 2


def test_delete_month_facts_can_scope_by_source_report_id():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                YouTubeChannelORM(
                    id=uuid4(),
                    youtube_channel_id="channel-rec",
                    channel_name="Reconciliation Allocation",
                    cms_status="OUTSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=uuid4(),
                    youtube_channel_id="channel-connector",
                    channel_name="Connector Allocation",
                    cms_status="OUTSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
            ]
        )
        session.commit()

        repository = SqlAlchemyRevenueFactRepository(session)
        repository.record_fact(
            month="2026-03",
            youtube_channel_id="channel-rec",
            source_kind="ALLOCATION",
            source_report_id="reconciliation_workflow:outside_cms_allocation",
            gross_revenue_usd=Decimal("42.00"),
            net_revenue_usd=None,
            views=0,
            watch_time_minutes=Decimal("0"),
            confidence_score=Decimal("0.9000"),
            actor_user_id=USER_ID,
        )
        repository.record_fact(
            month="2026-03",
            youtube_channel_id="channel-connector",
            source_kind="ALLOCATION",
            source_report_id="connector:allocation_report",
            gross_revenue_usd=Decimal("84.00"),
            net_revenue_usd=None,
            views=0,
            watch_time_minutes=Decimal("0"),
            confidence_score=Decimal("0.9000"),
            actor_user_id=USER_ID,
        )
        session.commit()

        deleted = repository.delete_month_facts(
            month="2026-03",
            source_kind="ALLOCATION",
            source_report_id="reconciliation_workflow:outside_cms_allocation",
            youtube_channel_ids={"channel-rec", "channel-connector"},
        )
        session.commit()

        assert deleted == 1
        assert (
            repository.list_channel_month_facts(month="2026-03", youtube_channel_id="channel-rec")
            == []
        )
        connector_facts = repository.list_channel_month_facts(
            month="2026-03", youtube_channel_id="channel-connector"
        )
        assert len(connector_facts) == 1
        assert connector_facts[0].source_report_id == "connector:allocation_report"
        assert connector_facts[0].gross_revenue_usd == Decimal("84.000000")


def test_revenue_fact_rejects_locked_month_in_bound_tenant():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                YouTubeChannelORM(
                    id=uuid4(),
                    tenant_id=OTHER_TENANT_ID,
                    youtube_channel_id="other-tenant-channel",
                    channel_name="Other Tenant Channel",
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                FinanceMonthCloseORM(
                    tenant_id=OTHER_TENANT_ID,
                    month="2026-03",
                    status="LOCKED",
                    locked_by=UUID(USER_ID),
                    allocation_rule_payload={},
                ),
            ]
        )
        session.commit()

        repository = SqlAlchemyRevenueFactRepository(session, tenant_id=OTHER_TENANT_ID)
        with pytest.raises(RevenueFactLockedMonthError):
            repository.record_fact(
                month="2026-03",
                youtube_channel_id="other-tenant-channel",
                source_kind="YOUTUBE_CMS",
                source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=None,
                views=1,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9000"),
                actor_user_id=USER_ID,
            )

        assert session.get(FinanceMonthCloseORM, (DEFAULT_TENANT_ID, "2026-03")) is None
