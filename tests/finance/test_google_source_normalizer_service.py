"""SQLite-backed service flow tests for GoogleSourceNormalizer."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.tenant_models import TenantBase
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError

# NOTE: source_models.py registers GoogleRevenueSourceRowORM and CurrencyORM
# on FinanceBase.metadata (see backend/ums_smart_revenue/db/source_models.py
# module docstring). There is no separate SourceBase to create; importing
# source_models is unnecessary because FinanceBase.metadata.create_all()
# already covers those tables.

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def _make_engine_and_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine, Session(engine)


def test_normalize_month_raises_validation_error_for_invalid_month_format():
    _, session = _make_engine_and_session()
    with session:
        normalizer = GoogleSourceNormalizer(session)
        with pytest.raises(RevenueFactValidationError, match="month must use YYYY-MM"):
            normalizer.normalize_month(month="2026-13", actor_user_id=ACTOR_USER_ID)
