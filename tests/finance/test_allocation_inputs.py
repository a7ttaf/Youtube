from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    """Create an in-memory SQLite engine with org and finance schemas."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed(session):
    """Seed a channel, verified account link, revenue fact and account deduction."""
    session.add(
        YouTubeChannelORM(
            id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
            channel_name="A", active=True,
        )
    )
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
            content_owner_id="owner-1", verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            effective_month_start="2026-01",
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
            active=True, effective_month_start="2026-01",
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH,
            youtube_channel_id="chA", source_kind="ADSENSE",
            gross_revenue_usd=Decimal("500.00"),
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH,
            component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-1",
            amount_usd=Decimal("100.00"), currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            component_key="acct-ded-1", raw_payload={},
        )
    )
    session.commit()


def test_compute_month_account_allocation_matches_endpoint_inputs(tmp_path):
    """The service produces the same allocation the PR-1 endpoint computed inline."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
        )
    assert result.allocation_method == "gross_revenue_proportional"
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.adsense_account_id == "pub-1"
    assert line.youtube_channel_id == "chA"
    assert line.allocated_amount_usd == Decimal("100.000000")
    assert line.net_applicable is True
    assert result.unallocated == ()


def test_post_tax_uses_source_net_basis(tmp_path):
    """post_tax weights the split by source net_revenue_usd, not gross."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        # Add a second verified channel (chB) + facts so the split is observable.
        session.add(
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chB",
                channel_name="B", active=True,
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chB", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            )
        )
        # chA: gross 500 / net 300; chB: gross 500 / net 100 -> net split 75/25.
        session.execute(
            MonthlyChannelRevenueFactORM.__table__.update()
            .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
            .values(net_revenue_usd=Decimal("300.00"))
        )
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                youtube_channel_id="chB", source_kind="ADSENSE",
                gross_revenue_usd=Decimal("500.00"), net_revenue_usd=Decimal("100.00"),
            )
        )
        session.commit()
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
            allocation_method="post_tax_revenue_proportional",
        )
    assert result.allocation_method == "post_tax_revenue_proportional"
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    # $100 deduction split by net 300:100 -> 75 / 25 (NOT the 50/50 gross split).
    assert by_channel == {"chA": Decimal("75.000000"), "chB": Decimal("25.000000")}


def test_post_tax_omits_key_with_any_null_net(tmp_path):
    """A (channel, source_kind) with any null-net fact is dropped -> fail closed."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)  # chA fact has net_revenue_usd = None (not set in _seed)
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
            allocation_method="post_tax_revenue_proportional",
        )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_MISSING"
