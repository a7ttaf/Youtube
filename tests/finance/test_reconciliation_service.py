"""Service tests for the smart revenue reconciliation workflow (SQLite)."""
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    ContentOwnerChannelLinkORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.explanations import REVENUE_RECONCILIATION_METRIC
from ums_smart_revenue.finance.net_revenue import build_channel_net_revenue_summary
from ums_smart_revenue.finance.reconciliation_service import (
    MonthLockedError,
    ReconciliationWorkflowService,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactSourceKind,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-03"
ACTOR_ID = "00000000-0000-0000-0000-0000000d0001"
DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)


def _actor() -> UserPrincipal:
    """Create a finance viewer actor for service tests."""
    return UserPrincipal(
        user_id=ACTOR_ID,
        email="recon@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )


def _engine():
    """Create a fresh in-memory SQLite schema with org + finance tables."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ExplanationBase.metadata.create_all(engine)
    return engine


def _add_channel(session, channel_id, *, cms_status="INSIDE_CMS"):
    """Insert one active YouTube channel."""
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            youtube_channel_id=channel_id,
            channel_name=channel_id.upper(),
            cms_status=cms_status,
            revenue_required=True,
            active=True,
        )
    )


def _add_cms_fact(session, channel_id, gross):
    """Insert a YOUTUBE_CMS gross fact with no net (component-derived path)."""
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            month=MONTH,
            youtube_channel_id=channel_id,
            source_kind=RevenueFactSourceKind.YOUTUBE_CMS.value,
            gross_revenue_usd=Decimal(gross),
            net_revenue_usd=None,
            views=0,
            watch_time_minutes=Decimal("0"),
            confidence_score=Decimal("0.90"),
            imported_by=UUID(ACTOR_ID),
        )
    )


def _add_payment(session, account, amount, *, status="PAID"):
    """Insert one AdSense payment."""
    session.add(
        AdSensePaymentORM(
            id=uuid4(), month=MONTH, payment_name="mar", source_account_id=account,
            payment_date=date(2026, 4, 21), payment_amount=Decimal(amount),
            payment_currency="USD", payment_status=status, raw_payload={},
            source_report_id=None, imported_by=UUID(ACTOR_ID),
        )
    )


def _add_bank(session, amount_usd, fx):
    """Insert one bank reconciliation entry."""
    session.add(
        BankReconciliationEntryORM(
            id=uuid4(), month=MONTH, bank_reference="BANK-1",
            bank_received_date=date(2026, 4, 25),
            bank_received_amount=Decimal(amount_usd), bank_received_currency="USD",
            bank_received_amount_usd=Decimal(amount_usd),
            transfer_fee_usd=Decimal("0.00"), fx_difference_usd=Decimal(fx),
            recorded_by=UUID(ACTOR_ID),
        )
    )


def _lock_month(session):
    """Lock the finance month."""
    session.add(
        FinanceMonthCloseORM(
            tenant_id=DEFAULT_TENANT_ID, month=MONTH, status="LOCKED",
            allocation_rule_payload={},
        )
    )


def _service(session):
    """Build the reconciliation service with an in-memory audit sink."""
    return ReconciliationWorkflowService(session, audit_sink=InMemoryAuditSink())


def _seed_standard(session):
    """Seed one INSIDE_CMS channel with gross + AdSense + bank for a full run."""
    _add_channel(session, "c1")
    _add_cms_fact(session, "c1", "100")
    _add_payment(session, "pub-1", "80")
    _add_bank(session, "60", "5")
    session.commit()


def test_run_persists_components_and_explanations():
    """A full run writes typed components + a reconciliation explanation."""
    engine = _engine()
    with Session(engine) as session:
        _seed_standard(session)
        sink = InMemoryAuditSink()
        svc = ReconciliationWorkflowService(
            session, audit_sink=sink,
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )
        result = svc.run(month=MONTH, actor=_actor(), reason="monthly")
        session.commit()

        comps = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )
        recon = [c for c in comps if c.source_table == "reconciliation_workflow"]
        kinds = {c.component_kind for c in recon}
        persisted = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.month == MONTH,
                NumberExplanationORM.entity_type == "channel",
                NumberExplanationORM.entity_id == "c1",
                NumberExplanationORM.metric == REVENUE_RECONCILIATION_METRIC,
            )
        ).one_or_none()
    assert result.month == MONTH
    # tax (15) + yt fee + adsense->bank fee + fx all > 0 for these inputs.
    assert {"TAX", "TRANSFER_FEE", "FX_VARIANCE"} <= kinds
    assert sink.records[-1].event_type == "REVENUE_RECONCILED"
    assert persisted is not None


def test_run_rejects_locked_month():
    """A LOCKED finance month must refuse the run, fail-closed (no audit)."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _lock_month(session)
        session.commit()
        sink = InMemoryAuditSink()
        svc = ReconciliationWorkflowService(session, audit_sink=sink)
        with pytest.raises(MonthLockedError):
            svc.run(month=MONTH, actor=_actor(), reason="r")
        assert sink.records == []


def test_recompute_is_idempotent():
    """A second run replaces, never duplicates, reconciliation components."""
    engine = _engine()
    with Session(engine) as session:
        _seed_standard(session)
        svc = _service(session)
        svc.run(month=MONTH, actor=_actor(), reason="r1")
        session.commit()
        svc.run(month=MONTH, actor=_actor(), reason="r2")
        session.commit()
        comps = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )
        recon = [c for c in comps if c.source_table == "reconciliation_workflow"]
    keys = [c.component_key for c in recon]
    assert len(keys) == len(set(keys))


def test_reconciliation_deductions_feed_net_revenue():
    """The reconciliation TAX component reduces the channel's derived net."""
    engine = _engine()
    with Session(engine) as session:
        _seed_standard(session)
        svc = ReconciliationWorkflowService(
            session, audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )
        svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        fact_repo = SqlAlchemyRevenueFactRepository(session)
        facts = fact_repo.list_channel_month_facts(month=MONTH, youtube_channel_id="c1")
        comps = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )
        summary = build_channel_net_revenue_summary(
            facts=facts,
            manual_overrides=[],
            month=MONTH,
            youtube_channel_id="c1",
            deduction_components=comps,
        )
    # 0.5 * 100 * 0.30 = 15 tax; the resolver SELECTS the source-aligned
    # reconciliation TAX row so net drops from 100 to 85.
    assert summary.net_revenue_usd == Decimal("85.000000")
    # The reconciliation TAX component is source-aligned to YOUTUBE_CMS.
    tax = [
        c for c in comps
        if c.component_kind == "TAX" and c.source_table == "reconciliation_workflow"
    ]
    assert tax and tax[0].source_system == "youtube_reporting"


class _FixedShareProvider:
    """Test provider that returns a fixed US-view share for every channel."""

    def __init__(self, share):
        self._share = share

    def us_view_share(self, month, youtube_channel_id):
        """Return the fixed configured share."""
        return self._share


def test_outside_cms_one_to_one_writes_allocation_fact():
    """An OUTSIDE_CMS channel mapped 1:1 to a PAID account gets an ALLOCATION fact."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-9", "42")
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
                adsense_account_id="pub-9", content_owner_id="owner-9",
                verification_status="VERIFIED", provenance_kind="MANUAL",
                provenance_payload={}, effective_month_start=MONTH,
                effective_month_end=None,
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
                content_owner_id="owner-9", youtube_channel_id="outside-1",
                provenance_kind="MANUAL", active=True,
                effective_month_start=MONTH, effective_month_end=None,
            )
        )
        session.commit()
        svc = _service(session)
        svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )
    alloc = [f for f in facts if f.source_kind == RevenueFactSourceKind.ALLOCATION.value]
    assert len(alloc) == 1
    assert alloc[0].gross_revenue_usd == Decimal("42")


def test_outside_cms_ambiguous_account_skips_and_warns():
    """An account mapped to many no-gross OUTSIDE_CMS channels writes no fact + warns."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-a", cms_status="OUTSIDE_CMS")
        _add_channel(session, "outside-b", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-7", "30")
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
                adsense_account_id="pub-7", content_owner_id="owner-7",
                verification_status="VERIFIED", provenance_kind="MANUAL",
                provenance_payload={}, effective_month_start=MONTH,
                effective_month_end=None,
            )
        )
        for channel in ("outside-a", "outside-b"):
            session.add(
                ContentOwnerChannelLinkORM(
                    id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
                    content_owner_id="owner-7", youtube_channel_id=channel,
                    provenance_kind="MANUAL", active=True,
                    effective_month_start=MONTH, effective_month_end=None,
                )
            )
        session.commit()
        svc = _service(session)
        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        facts_a = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-a"
        )
    assert facts_a == []
    assert any(w["code"] == "MISSING_REVENUE_SOURCE" for w in result.warnings)
