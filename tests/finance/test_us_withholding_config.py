from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import SecurityBase, UserORM, UsWithholdingRateConfigORM
from ums_smart_revenue.finance.us_withholding_config import (
    SqlAlchemyUsWithholdingConfigRepository,
    validate_us_withholding_rate,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT_ID = UUID(UMS_TENANT_ID)
USER_ID = UUID("00000000-0000-0000-0000-000000088002")


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'wh.db').as_posix()}")
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            UserORM(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="finance@example.com",
                display_name="Finance",
            )
        )
        db.commit()
        yield db


def test_validate_us_withholding_rate_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="between 0 and"):
        validate_us_withholding_rate(Decimal("0.31"))


def test_get_effective_rate_returns_none_without_config(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    assert repo.get_effective_rate(tenant_id=TENANT_ID, as_of=date(2026, 4, 30)) is None


def test_get_effective_rate_picks_latest_effective_from(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        effective_from=date(2026, 1, 1),
        rate=Decimal("0.15"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.20"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()
    snapshot = repo.get_effective_rate(tenant_id=TENANT_ID, as_of=date(2026, 4, 15))
    assert snapshot is not None
    assert snapshot.rate == Decimal("0.20")
    older = repo.get_effective_rate(tenant_id=TENANT_ID, as_of=date(2026, 2, 1))
    assert older is not None
    assert older.rate == Decimal("0.15")


def test_no_default_rate_row_seeded(session: Session) -> None:
    count = session.query(UsWithholdingRateConfigORM).count()
    assert count == 0
