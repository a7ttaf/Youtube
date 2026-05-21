from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)


def test_number_explanation_model_persists_components_and_warnings():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ExplanationBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            NumberExplanationORM(
                id=uuid4(),
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric="adjusted_gross_revenue_usd",
                value=Decimal("1125.50"),
                currency="USD",
                formula=(
                    "baseline_gross_revenue_usd + approved_manual_override_total_usd"
                ),
                confidence="HIGH",
                components=[{"key": "baseline_gross_revenue_usd", "value": "1000"}],
                warnings=[{"code": "PENDING_MANUAL_OVERRIDES"}],
            )
        )
        session.commit()
        explanation = session.scalars(select(NumberExplanationORM)).one()

    assert explanation.metric == "adjusted_gross_revenue_usd"
    assert explanation.value == Decimal("1125.50")
    assert explanation.components == [
        {"key": "baseline_gross_revenue_usd", "value": "1000"}
    ]
    assert explanation.warnings == [{"code": "PENDING_MANUAL_OVERRIDES"}]
