"""Locked-month gate: closed books fail loud even with zero source rows."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def test_normalize_month_raises_locked_month_error_with_zero_source_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    # source_models registers on FinanceBase.metadata -- no separate base required.
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        session.add(TenantORM(id=tenant_id, slug="t-locked", display_name="T Locked"))
        session.add(
            FinanceMonthCloseORM(
                tenant_id=tenant_id,
                month="2026-04",
                status="LOCKED",
                allocation_rule_payload={},
            )
        )
        session.commit()

        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        with pytest.raises(RevenueFactLockedMonthError):
            normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
