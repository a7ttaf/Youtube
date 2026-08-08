from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, RevenueManualOverrideORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM

CREATOR_ID = UUID("00000000-0000-0000-0000-000000008001")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000008002")


def build_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    FinanceBase.metadata.create_all(engine)
    return engine


def test_revenue_manual_override_model_persists_pending_adjustment():
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


def test_revenue_manual_override_model_declares_channel_foreign_key():
    foreign_key = next(
        (
            constraint
            for constraint in RevenueManualOverrideORM.__table__.foreign_key_constraints
            if constraint.name == "fk_revenue_manual_overrides_tenant_channel"
        ),
        None,
    )
    assert foreign_key is not None, "fk_revenue_manual_overrides_tenant_channel missing"

    assert [column.name for column in foreign_key.columns] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert [element.column.name for element in foreign_key.elements] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert foreign_key.ondelete == "RESTRICT"
