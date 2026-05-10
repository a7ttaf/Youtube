from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, MonthlyChannelRevenueFactORM


USER_ID = UUID("00000000-0000-0000-0000-000000007001")


def test_monthly_channel_revenue_fact_model_persists_canonical_values():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
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
