"""Read-switch resolver: lock-aware snapshot-vs-live selection + reconstruction."""
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
    account_allocation_disclosure_token,
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
MONTH = "2026-04"
ACTOR = str(TENANT)


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    session = Session(engine)
    session.add(TenantORM(
        id=TENANT, slug="ums", display_name="UMS", primary_currency="USD", status="ACTIVE",
    ))
    session.commit()
    return session


def _add_account(session, *, account, channel, gross, deduction, mapped):
    """Seed one ACCOUNT deduction over one channel (ADSENSE gross), optionally mapped."""
    session.add(YouTubeChannelORM(
        id=uuid4(), tenant_id=TENANT, youtube_channel_id=channel, channel_name=channel, active=True,
    ))
    session.flush()  # cross-registry composite FK: channel before the dependent fact
    session.add(MonthlyChannelRevenueFactORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id=channel,
        source_kind="ADSENSE", gross_revenue_usd=Decimal(gross), net_revenue_usd=None,
    ))
    session.add(DeductionComponentORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
        scope_kind="ACCOUNT", scope_id=account, amount_usd=Decimal(deduction),
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", component_key=f"ad-{account}",
        raw_payload={},
    ))
    if mapped:
        owner = f"owner-{account}"
        session.add(AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id=account, content_owner_id=owner,
            verification_status="VERIFIED", provenance_kind="OPERATOR_ASSERTED",
            provenance_payload={}, effective_month_start="2026-01",
        ))
        session.add(ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id=owner, youtube_channel_id=channel,
            provenance_kind="SOURCE_ROW", active=True, effective_month_start="2026-01",
        ))


def _close(session, status):
    # commit_allocation() auto-creates an OPEN finance_month_close row via
    # get_or_create_month_close_row, so after a snapshot commit the (tenant, month)
    # row already exists. Update it in place rather than blindly inserting (the
    # UNIQUE(tenant_id, month) constraint would otherwise reject a second row).
    existing = session.query(FinanceMonthCloseORM).filter_by(
        tenant_id=TENANT, month=MONTH,
    ).one_or_none()
    if existing is None:
        session.add(FinanceMonthCloseORM(
            tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
        ))
    else:
        existing.status = status
    session.commit()


def _repos(session):
    return (
        SqlAlchemyCommittedAllocationRepository(session),
        SqlAlchemyDeductionComponentRepository(session),
        SqlAlchemyRevenueFactRepository(session),
        SqlAlchemyChannelAccountLinkRepository(session),
    )


def _commit(session, *, status_after="OPEN"):
    """Seed one mapped account, commit a snapshot, then set the close status."""
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    _close(session, status_after)


def _resolve(session, *, adsense_account_id=None):
    committed, ded, rev, link = _repos(session)
    return resolve_month_account_allocation(
        month=MONTH, session=session, deduction_repository=ded, revenue_repository=rev,
        link_repository=link, committed_repository=committed,
        adsense_account_id=adsense_account_id,
    )


def test_locked_with_snapshot_uses_committed(tmp_path):
    """LOCKED month with a committed run -> committed_snapshot provenance + snapshot lines."""
    session = _session(tmp_path)
    _commit(session, status_after="LOCKED")
    result, prov = _resolve(session)
    assert prov.source == "committed_snapshot"
    assert prov.commit_version == 1
    assert prov.run_id is not None
    assert len(result.lines) == 1
    assert result.lines[0].youtube_channel_id == "chA"
    assert result.summary.allocated_total_usd == Decimal("100.000000")


def test_open_month_uses_live_compute(tmp_path):
    """OPEN month -> live_compute even when a snapshot exists."""
    session = _session(tmp_path)
    _commit(session, status_after="OPEN")
    _result, prov = _resolve(session)
    assert prov.source == "live_compute"
    assert prov.commit_version is None


def test_no_close_row_uses_live_compute(tmp_path):
    """No close row -> treated as open -> live_compute."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    _result, prov = _resolve(session)
    assert prov.source == "live_compute"


def test_locked_without_snapshot_falls_back_to_live(tmp_path):
    """LOCKED month with no committed run -> live_fallback (never errors)."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    _close(session, "LOCKED")
    result, prov = _resolve(session)
    assert prov.source == "live_fallback"
    assert len(result.lines) == 1


def test_reconstruction_equals_live_for_locked(tmp_path):
    """Rebuilt snapshot result equals the live result for the same frozen inputs."""
    session = _session(tmp_path)
    _commit(session, status_after="LOCKED")
    snap, _prov = _resolve(session)
    committed, ded, rev, link = _repos(session)
    live = compute_month_account_allocation(
        month=MONTH, deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )
    assert snap.lines == live.lines
    assert snap.unallocated == live.unallocated
    assert snap.summary == live.summary


def test_account_filter_matches_live_per_account(tmp_path):
    """LOCKED snapshot filtered to one account == live compute filtered to that account."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    _add_account(session, account="pub-2", channel="chB", gross="500.00", deduction="40.00", mapped=True)
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )
    _close(session, "LOCKED")
    for account in ("pub-1", "pub-2"):
        snap, prov = _resolve(session, adsense_account_id=account)
        live = compute_month_account_allocation(
            month=MONTH, deduction_repository=ded, revenue_repository=rev,
            link_repository=link, adsense_account_id=account,
        )
        assert prov.source == "committed_snapshot"
        assert snap.lines == live.lines
        assert snap.unallocated == live.unallocated
        assert snap.notes == live.notes == ()
        assert snap.summary == live.summary


def test_provenance_api_and_token(tmp_path):
    """allocation_provenance_to_api + disclosure token render committed vs live."""
    committed = AllocationProvenance(
        source="committed_snapshot", commit_version=3,
        committed_at=None, run_id=UUID(int=1),
    )
    api = allocation_provenance_to_api(committed)
    assert api["allocation_source"] == "committed_snapshot"
    assert api["committed_run"]["commit_version"] == 3
    assert allocation_provenance_to_api(AllocationProvenance(source="live_compute"))["committed_run"] is None
    assert account_allocation_disclosure_token(AllocationProvenance(source="live_compute")) == (
        "Account allocation: live compute"
    )
    assert account_allocation_disclosure_token(AllocationProvenance(source="live_fallback")) == (
        "Account allocation: live fallback"
    )
    assert "committed snapshot v3" in account_allocation_disclosure_token(committed)
