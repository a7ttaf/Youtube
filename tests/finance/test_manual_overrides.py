from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideValidationError,
    SqlAlchemyManualOverrideRepository,
)


USER_ID = "00000000-0000-0000-0000-000000011001"


def test_manual_override_repository_rejects_non_finite_adjustment_amount():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        repository = SqlAlchemyManualOverrideRepository(session)

        with pytest.raises(ManualOverrideValidationError, match="adjustment_revenue_usd must be a finite decimal"):
            repository.create_override(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                adjustment_revenue_usd=Decimal("NaN"),
                reason="Reject impossible finance amount",
                actor_user_id=USER_ID,
            )
