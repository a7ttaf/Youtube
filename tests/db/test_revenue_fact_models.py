from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM

USER_ID = UUID("00000000-0000-0000-0000-000000007001")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000007002")


def build_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        del connection_record
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    FinanceBase.metadata.create_all(engine)
    return engine


def test_monthly_channel_revenue_fact_model_persists_canonical_values():
    engine = build_engine()

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
        session.flush()
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id="cms-report-2026-03",
                gross_revenue_usd=Decimal("1234.56"),
                net_revenue_usd=Decimal("987.65"),
                views=250000,
                watch_time_minutes=Decimal("7200.50"),
                confidence_score=Decimal("0.9825"),
                imported_by=USER_ID,
            )
        )
        session.commit()
        fact = session.scalars(select(MonthlyChannelRevenueFactORM)).one()

    assert fact.month == "2026-03"
    assert fact.youtube_channel_id == "channel-tv-a"
    assert fact.source_kind == "YOUTUBE_CMS"
    assert fact.gross_revenue_usd == Decimal("1234.56")
    assert fact.views == 250000
    assert fact.imported_by == USER_ID


def test_monthly_channel_revenue_fact_model_declares_channel_foreign_key():
    foreign_key = next(
        constraint
        for constraint in MonthlyChannelRevenueFactORM.__table__.foreign_key_constraints
        if constraint.name == "fk_monthly_channel_revenue_facts_tenant_channel"
    )

    assert [column.name for column in foreign_key.columns] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert [element.column.name for element in foreign_key.elements] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert foreign_key.ondelete == "RESTRICT"
