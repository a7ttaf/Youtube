from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideLockedMonthError,
    ManualOverrideValidationError,
    SqlAlchemyManualOverrideRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

USER_ID = "00000000-0000-0000-0000-000000011001"
SECTOR_ID = UUID("00000000-0000-0000-0000-000000011101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000011201")
ACTIVE_CHANNEL_ID = UUID("00000000-0000-0000-0000-000000011301")
INACTIVE_CHANNEL_ID = UUID("00000000-0000-0000-0000-000000011302")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000011999")
DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)


def test_manual_override_repository_rejects_non_finite_adjustment_amount():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        repository = SqlAlchemyManualOverrideRepository(session)

        with pytest.raises(
            ManualOverrideValidationError,
            match="adjustment_revenue_usd must be a finite decimal",
        ):
            repository.create_override(
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                adjustment_revenue_usd=Decimal("NaN"),
                reason="Reject impossible finance amount",
                actor_user_id=USER_ID,
            )


def test_list_month_overrides_excludes_inactive_channels():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=ACTIVE_CHANNEL_ID,
                    youtube_channel_id="channel-active",
                    channel_name="Active Channel",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=INACTIVE_CHANNEL_ID,
                    youtube_channel_id="channel-inactive",
                    channel_name="Inactive Channel",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=False,
                ),
                RevenueManualOverrideORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-active",
                    adjustment_revenue_usd=Decimal("50.00"),
                    reason="Active channel adjustment",
                    status="APPROVED",
                    created_by=UUID(USER_ID),
                    approved_by=uuid4(),
                    approved_at=datetime(2026, 4, 24, tzinfo=UTC),
                    approval_reason="Approved",
                ),
                RevenueManualOverrideORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-inactive",
                    adjustment_revenue_usd=Decimal("75.00"),
                    reason="Inactive channel adjustment",
                    status="APPROVED",
                    created_by=UUID(USER_ID),
                    approved_by=uuid4(),
                    approved_at=datetime(2026, 4, 24, tzinfo=UTC),
                    approval_reason="Approved",
                ),
            ]
        )
        session.commit()

        repository = SqlAlchemyManualOverrideRepository(session)
        overrides = repository.list_month_overrides(month="2026-03")

    assert [override.youtube_channel_id for override in overrides] == ["channel-active"]


def test_manual_override_rejects_locked_month_in_bound_tenant():
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

        repository = SqlAlchemyManualOverrideRepository(
            session, tenant_id=OTHER_TENANT_ID
        )
        with pytest.raises(ManualOverrideLockedMonthError):
            repository.create_override(
                month="2026-03",
                youtube_channel_id="other-tenant-channel",
                adjustment_revenue_usd=Decimal("50.00"),
                reason="Should honor tenant lock",
                actor_user_id=USER_ID,
            )

        assert session.get(FinanceMonthCloseORM, (DEFAULT_TENANT_ID, "2026-03")) is None
