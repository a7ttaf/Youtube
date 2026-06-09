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
    DeductionComponentLockedMonthError,
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


def _add_cms_fact(
    session, channel_id, gross, *, source_kind=None, source_report_id=None
):
    """Insert a gross fact with no net (component-derived path)."""
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            month=MONTH,
            youtube_channel_id=channel_id,
            source_kind=source_kind or RevenueFactSourceKind.YOUTUBE_CMS.value,
            source_report_id=source_report_id,
            gross_revenue_usd=Decimal(gross),
            net_revenue_usd=None,
            views=0,
            watch_time_minutes=Decimal("0"),
            confidence_score=Decimal("0.90"),
            imported_by=UUID(ACTOR_ID),
        )
    )


def _add_payment(session, account, amount, *, status="PAID", currency="USD"):
    """Insert one AdSense payment."""
    session.add(
        AdSensePaymentORM(
            id=uuid4(), month=MONTH, payment_name="mar", source_account_id=account,
            payment_date=date(2026, 4, 21), payment_amount=Decimal(amount),
            payment_currency=currency, payment_status=status, raw_payload={},
            source_report_id=None, imported_by=UUID(ACTOR_ID),
        )
    )


def _link_account_channel(session, account, owner, channel):
    """Insert one VERIFIED account->owner->channel mapping for the test month."""
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
            adsense_account_id=account, content_owner_id=owner,
            verification_status="VERIFIED", provenance_kind="MANUAL",
            provenance_payload={}, effective_month_start=MONTH,
            effective_month_end=None,
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=DEFAULT_TENANT_ID,
            content_owner_id=owner, youtube_channel_id=channel,
            provenance_kind="MANUAL", active=True,
            effective_month_start=MONTH, effective_month_end=None,
        )
    )


def _add_bank(session, amount_usd, fx, *, reference="BANK-1"):
    """Insert one bank reconciliation entry."""
    session.add(
        BankReconciliationEntryORM(
            id=uuid4(), month=MONTH, bank_reference=reference,
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
    _link_account_channel(session, "pub-1", "owner-1", "c1")
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


def test_run_normalizes_repository_lock_race_to_month_locked_error():
    """Repository lock guards raised after preflight still surface as MonthLockedError."""
    engine = _engine()
    with Session(engine) as session:
        _seed_standard(session)
        sink = InMemoryAuditSink()
        svc = ReconciliationWorkflowService(session, audit_sink=sink)
        svc._deductions = _LockedDeductionRepository()

        with pytest.raises(MonthLockedError):
            svc.run(month=MONTH, actor=_actor(), reason="race")

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


def test_run_prunes_stale_reconciliation_explanations():
    """A recompute deletes channel explanations that left the current result."""
    engine = _engine()
    with Session(engine) as session:
        _seed_standard(session)
        session.add(
            NumberExplanationORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                month=MONTH,
                entity_type="channel",
                entity_id="stale-channel",
                metric=REVENUE_RECONCILIATION_METRIC,
                value=Decimal("99.00"),
                currency="USD",
                formula="stale",
                confidence="LOW",
                components=[],
                warnings=[],
            )
        )
        session.commit()

        _service(session).run(month=MONTH, actor=_actor(), reason="rerun")
        session.commit()

        stale = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.month == MONTH,
                NumberExplanationORM.entity_type == "channel",
                NumberExplanationORM.entity_id == "stale-channel",
                NumberExplanationORM.metric == REVENUE_RECONCILIATION_METRIC,
            )
        ).one_or_none()
        current = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.month == MONTH,
                NumberExplanationORM.entity_type == "channel",
                NumberExplanationORM.entity_id == "c1",
                NumberExplanationORM.metric == REVENUE_RECONCILIATION_METRIC,
            )
        ).one_or_none()

    assert stale is None
    assert current is not None


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


def test_run_uses_primary_revenue_fact_instead_of_summing_sources():
    """Multi-source channels reconcile from SOURCE_PRIORITY's primary fact only."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_cms_fact(
            session,
            "c1",
            "60",
            source_kind=RevenueFactSourceKind.ADSENSE.value,
        )
        _add_payment(session, "pub-1", "80")
        _link_account_channel(session, "pub-1", "owner-1", "c1")
        session.commit()

        svc = _service(session)
        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        comps = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )

    assert result.gross_total_usd == Decimal("100.000000")
    assert result.yt_adsense_fee_total_usd == Decimal("20.000000")
    tax = [
        c for c in comps
        if c.component_kind == "TRANSFER_FEE"
        and c.source_table == "reconciliation_workflow"
    ]
    assert tax and tax[0].source_system == "youtube_reporting"


def test_run_ignores_non_usd_paid_adsense_payments():
    """Only PAID USD payment amounts feed the AdSense reconciliation total."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-1", "80")
        _add_payment(session, "pub-eur", "50", currency="EUR")
        _link_account_channel(session, "pub-1", "owner-1", "c1")
        session.commit()

        result = _service(session).run(month=MONTH, actor=_actor(), reason="r")

    assert result.yt_adsense_fee_total_usd == Decimal("20.000000")


def test_run_deletes_stale_outside_cms_allocation_when_payment_invalidates():
    """Recompute removes old ALLOCATION facts when current state no longer qualifies."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-9", "42")
        _link_account_channel(session, "pub-9", "owner-9", "outside-1")
        session.commit()

        svc = _service(session)
        svc.run(month=MONTH, actor=_actor(), reason="first")
        payment = session.scalars(select(AdSensePaymentORM)).one()
        payment.payment_status = "PENDING"
        session.commit()

        svc.run(month=MONTH, actor=_actor(), reason="second")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )

    assert [
        f for f in facts
        if f.source_kind == RevenueFactSourceKind.ALLOCATION.value
    ] == []


def test_run_deletes_stale_allocation_when_channel_leaves_outside_cms():
    """Recompute removes ALLOCATION facts when a channel is no longer OUTSIDE_CMS."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-9", "42")
        _link_account_channel(session, "pub-9", "owner-9", "outside-1")
        session.commit()

        svc = _service(session)
        svc.run(month=MONTH, actor=_actor(), reason="first")
        channel = session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == "outside-1"
            )
        ).one()
        channel.cms_status = "INSIDE_CMS"
        session.commit()

        svc.run(month=MONTH, actor=_actor(), reason="second")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )

    assert [
        f for f in facts
        if f.source_kind == RevenueFactSourceKind.ALLOCATION.value
    ] == []


def test_run_preserves_connector_allocation_fact_when_no_payment_qualifies():
    """Recompute must not delete source-loaded ALLOCATION revenue facts."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_cms_fact(
            session,
            "outside-1",
            "42",
            source_kind=RevenueFactSourceKind.ALLOCATION.value,
            source_report_id="connector-allocation-report",
        )
        session.commit()

        _service(session).run(month=MONTH, actor=_actor(), reason="recompute")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )

    alloc = [f for f in facts if f.source_kind == RevenueFactSourceKind.ALLOCATION.value]
    assert len(alloc) == 1
    assert alloc[0].source_report_id == "connector-allocation-report"
    assert alloc[0].gross_revenue_usd == Decimal("42.000000")


def test_rerun_scopes_stale_allocation_delete_to_reconciliation_source_report():
    """Stale cleanup must pass the reconciliation ownership marker to deletion."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-9", "42")
        _link_account_channel(session, "pub-9", "owner-9", "outside-1")
        session.commit()

        svc = _service(session)
        svc.run(month=MONTH, actor=_actor(), reason="first")
        session.commit()

        channel = session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == "outside-1"
            )
        ).one()
        channel.cms_status = "INSIDE_CMS"
        session.commit()

        delete_calls = []
        original_delete = svc._facts.delete_month_facts

        def spy_delete_month_facts(**kwargs):
            delete_calls.append(kwargs)
            return original_delete(**kwargs)

        svc._facts.delete_month_facts = spy_delete_month_facts

        svc.run(month=MONTH, actor=_actor(), reason="second")
        session.commit()

    assert delete_calls
    assert (
        delete_calls[0].get("source_report_id")
        == "reconciliation_workflow:outside_cms_allocation"
    )


def test_run_includes_connector_allocation_fact_in_reconciliation_basis():
    """Source-loaded ALLOCATION facts participate in residual reconciliation."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_cms_fact(
            session,
            "outside-1",
            "40",
            source_kind=RevenueFactSourceKind.ALLOCATION.value,
            source_report_id="connector-allocation-report",
        )
        _add_payment(session, "pub-alloc", "30")
        _link_account_channel(session, "pub-alloc", "owner-alloc", "outside-1")
        session.commit()

        svc = ReconciliationWorkflowService(
            session,
            audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0")),
        )
        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        comps = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )

    assert [line.youtube_channel_id for line in result.channels] == ["outside-1"]
    assert result.gross_total_usd == Decimal("40.000000")
    assert result.yt_adsense_fee_total_usd == Decimal("10.000000")
    transfer_fee = [
        c
        for c in comps
        if c.scope_id == "outside-1" and c.component_kind == "TRANSFER_FEE"
    ]
    assert transfer_fee and transfer_fee[0].source_system == "reconciliation"


class _LockedDeductionRepository:
    """Test double that simulates a repository month-lock race."""

    def upsert_components(
        self, *, month, components, replace_source_tables=None
    ):
        """Raise the same locked-month error as the real repository."""
        raise DeductionComponentLockedMonthError("race locked")


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
        _link_account_channel(session, "pub-9", "owner-9", "outside-1")
        session.commit()
        svc = ReconciliationWorkflowService(
            session,
            audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )
        svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )
        components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )
        summary = build_channel_net_revenue_summary(
            facts=facts,
            manual_overrides=[],
            month=MONTH,
            youtube_channel_id="outside-1",
            deduction_components=components,
        )
    alloc = [f for f in facts if f.source_kind == RevenueFactSourceKind.ALLOCATION.value]
    tax_components = [
        c for c in components
        if c.scope_id == "outside-1" and c.component_kind == "TAX"
    ]
    assert len(alloc) == 1
    assert alloc[0].gross_revenue_usd == Decimal("42")
    assert tax_components == []
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None


def test_outside_cms_allocation_excluded_from_cms_residual_fee_basis():
    """Pass-through OUTSIDE_CMS payments do not reduce CMS residual attribution."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-cms", "80")
        _link_account_channel(session, "pub-cms", "owner-cms", "c1")
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-out", "40")
        _link_account_channel(session, "pub-out", "owner-out", "outside-1")
        session.commit()
        svc = ReconciliationWorkflowService(
            session,
            audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )

        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )

    assert [line.youtube_channel_id for line in result.channels] == ["c1"]
    assert result.gross_total_usd == Decimal("100.000000")
    assert result.yt_adsense_fee_total_usd == Decimal("5.000000")
    outside_components = [c for c in components if c.scope_id == "outside-1"]
    assert outside_components == []


def test_outside_cms_allocation_skips_unscoped_bank_basis():
    """Filtered AdSense totals must not be compared with unfiltered bank cash."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-cms", "80")
        _link_account_channel(session, "pub-cms", "owner-cms", "c1")
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-out", "40")
        _link_account_channel(session, "pub-out", "owner-out", "outside-1")
        _add_bank(session, "60", "5", reference="BANK-CMS")
        _add_bank(session, "40", "0", reference="BANK-OUT")
        session.commit()
        svc = ReconciliationWorkflowService(
            session,
            audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )

        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )

    assert result.adsense_bank_fee_total_usd == Decimal("0.000000")
    assert result.fx_total_usd == Decimal("0.000000")
    assert any(w["code"] == "BANK_BASIS_UNSCOPED" for w in result.warnings)
    bank_components = [
        c for c in components
        if c.component_key.endswith((":adsense_bank_fee", ":fx_variance"))
    ]
    assert bank_components == []


def test_outside_cms_multiple_accounts_same_channel_sum_before_allocation():
    """Multiple 1:1 AdSense accounts for one OUTSIDE_CMS channel are aggregated."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-9", "42")
        _add_payment(session, "pub-10", "8")
        _link_account_channel(session, "pub-9", "owner-9", "outside-1")
        _link_account_channel(session, "pub-10", "owner-10", "outside-1")
        session.commit()

        _service(session).run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )

    alloc = [f for f in facts if f.source_kind == RevenueFactSourceKind.ALLOCATION.value]
    assert len(alloc) == 1
    assert alloc[0].gross_revenue_usd == Decimal("50")


def test_mixed_source_backed_account_does_not_allocate_outside_channel():
    """An account must be wholly 1:1 OUTSIDE_CMS before writing ALLOCATION."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_channel(session, "outside-1", cms_status="OUTSIDE_CMS")
        _add_payment(session, "pub-mixed", "80")
        _link_account_channel(session, "pub-mixed", "owner-mixed-cms", "c1")
        _link_account_channel(session, "pub-mixed", "owner-mixed-out", "outside-1")
        session.commit()

        result = _service(session).run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="outside-1"
        )

    assert facts == []
    assert any(w["code"] == "MISSING_REVENUE_SOURCE" for w in result.warnings)


def test_unmapped_adsense_payment_excluded_from_cms_residual_total():
    """Unverified AdSense payments cannot clamp fees for mapped source-backed channels."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-cms", "80")
        _link_account_channel(session, "pub-cms", "owner-cms", "c1")
        _add_payment(session, "pub-unmapped", "50")
        session.commit()

        result = _service(session).run(month=MONTH, actor=_actor(), reason="r")

    assert result.yt_adsense_fee_total_usd == Decimal("20.000000")
    assert any(w["code"] == "MISSING_REVENUE_SOURCE" for w in result.warnings)


def test_outside_cms_warnings_persist_to_reconciliation_explanation():
    """Returned outside-CMS warnings are also stored in the channel explanation."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-cms", "80")
        _link_account_channel(session, "pub-cms", "owner-cms", "c1")
        _add_payment(session, "pub-unmapped", "50")
        session.commit()

        result = _service(session).run(month=MONTH, actor=_actor(), reason="r")
        session.commit()

        persisted = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.month == MONTH,
                NumberExplanationORM.entity_type == "channel",
                NumberExplanationORM.entity_id == "c1",
                NumberExplanationORM.metric == REVENUE_RECONCILIATION_METRIC,
            )
        ).one()

    assert any(w["code"] == "MISSING_REVENUE_SOURCE" for w in result.warnings)
    assert any(
        w["code"] == "MISSING_REVENUE_SOURCE" for w in persisted.warnings
    )


def test_manual_upload_primary_fact_reconciles_without_source_mapping_error():
    """MANUAL_UPLOAD reconciliation components remain net-revenue aligned."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(
            session,
            "c1",
            "100",
            source_kind=RevenueFactSourceKind.MANUAL_UPLOAD.value,
        )
        _add_payment(session, "pub-cms", "80")
        _link_account_channel(session, "pub-cms", "owner-cms", "c1")
        session.commit()
        svc = ReconciliationWorkflowService(
            session,
            audit_sink=InMemoryAuditSink(),
            us_view_share_provider=_FixedShareProvider(Decimal("0.5")),
        )

        result = svc.run(month=MONTH, actor=_actor(), reason="r")
        components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )
        facts = SqlAlchemyRevenueFactRepository(session).list_channel_month_facts(
            month=MONTH, youtube_channel_id="c1"
        )
        summary = build_channel_net_revenue_summary(
            facts=facts,
            manual_overrides=[],
            deduction_components=components,
            month=MONTH,
            youtube_channel_id="c1",
        )

    assert result.gross_total_usd == Decimal("100.000000")
    assert result.us_tax_total_usd == Decimal("15.000000")
    assert result.yt_adsense_fee_total_usd == Decimal("5.000000")
    tax_components = [c for c in components if c.component_kind == "TAX"]
    assert {c.source_system for c in tax_components} == {"manual_upload"}
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.net_revenue_usd == Decimal("85.000000")


def test_non_usd_paid_payment_skips_unscoped_bank_basis():
    """Skipping PAID non-USD AdSense rows also makes month-level bank totals unsafe."""
    engine = _engine()
    with Session(engine) as session:
        _add_channel(session, "c1")
        _add_cms_fact(session, "c1", "100")
        _add_payment(session, "pub-usd", "80")
        _link_account_channel(session, "pub-usd", "owner-usd", "c1")
        _add_payment(session, "pub-eur", "40", currency="EUR")
        _add_bank(session, "60", "5", reference="BANK-USD")
        _add_bank(session, "40", "0", reference="BANK-EUR")
        session.commit()

        result = _service(session).run(month=MONTH, actor=_actor(), reason="r")
        components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
            month=MONTH
        )

    assert result.adsense_bank_fee_total_usd == Decimal("0.000000")
    assert result.fx_total_usd == Decimal("0.000000")
    assert any(w["code"] == "BANK_BASIS_UNSCOPED" for w in result.warnings)
    bank_components = [
        c for c in components
        if c.component_key.endswith((":adsense_bank_fee", ":fx_variance"))
    ]
    assert bank_components == []


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
