from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, RevenueManualOverrideORM


CREATOR_ID = UUID("00000000-0000-0000-0000-000000008001")


def test_revenue_manual_override_model_persists_pending_adjustment():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            RevenueManualOverrideORM(
                id=uuid4(),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                adjustment_revenue_usd=Decimal("125.50"),
                reason="Correct CMS transfer-fee allocation",
                created_by=CREATOR_ID,
            )
        )
        session.commit()
        override = session.scalars(select(RevenueManualOverrideORM)).one()

    assert override.month == "2026-03"
    assert override.youtube_channel_id == "channel-tv-a"
    assert override.adjustment_revenue_usd == Decimal("125.50")
    assert override.status == "PENDING"
    assert override.created_by == CREATOR_ID
