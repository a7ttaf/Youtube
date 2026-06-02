"""Repository behavior for committed account allocation (write path)."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
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
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommittedAllocationIdempotencyConflictError,
    CommittedAllocationLockedMonthError,
    CommittedAllocationValidationError,
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
MONTH = "2026-04"
ACTOR = str(TENANT)  # any UUID-literal actor; the repo maps it via actor_identity_uuid


def _session(tmp_path) -> Session:
    """Fresh SQLite session with org + finance schema and FK enforcement on."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # YouTubeChannelORM lives on OrgBase; the finance rows live on FinanceBase; and
    # `tenants` (the FK parent for deduction/link rows) lives on TenantBase, a
    # separate base. All three schemas must exist, and the tenant parent row must be
    # inserted before any tenant-scoped row under FK enforcement.
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    session = Session(engine)
    session.add(TenantORM(
        id=TENANT, slug="ums", display_name="UMS",
        primary_currency="USD", status="ACTIVE",
    ))
    session.commit()
    return session


def _seed_account_deduction(session, *, mapped: bool, status: str = "OPEN") -> None:
    """Seed one ACCOUNT DEDUCTION (pub-1, 100.00) over channel chA (ADSENSE 1000.00).

    Field-for-field the shape of `_seed_missing_net_with_components` in
    tests/api/test_exports_account_allocation.py, reduced to a single ACCOUNT
    component. With mapped=True the account resolves to chA via a VERIFIED
    Adsense->owner link plus an active owner->channel link, so the compute returns
    exactly one fully-allocated line and zero unallocated issues. With mapped=False
    the two link rows are omitted, so pub-1 resolves to no channel and the compute
    yields one UnallocatedIssue. `status` seeds the finance-month close row
    ("OPEN" or "LOCKED") so the OPEN-month guard can be exercised without going
    through the month-close readiness path.
    """
    session.add(YouTubeChannelORM(
        id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
        channel_name="A", active=True,
    ))
    # Flush the channel before the fact: monthly_channel_revenue_facts has a
    # composite FK (tenant_id, youtube_channel_id) -> youtube_channels that crosses
    # the Org/Finance registries, so the unit-of-work does NOT order the channel
    # insert before the dependent fact on its own. Required under FK enforcement.
    session.flush()
    session.add(MonthlyChannelRevenueFactORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
        source_kind="ADSENSE", gross_revenue_usd=Decimal("1000.00"),
        net_revenue_usd=None,
    ))
    session.add(DeductionComponentORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
        scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", component_key="ad-1",
        raw_payload={},
    ))
    if mapped:
        session.add(AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
            content_owner_id="owner-1", verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            effective_month_start="2026-01",
        ))
        session.add(ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW", active=True,
            effective_month_start="2026-01",
        ))
    session.add(FinanceMonthCloseORM(
        tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
    ))
    session.commit()


def _repos(session):
    """Return (committed_repo, deduction_repo, revenue_repo, link_repo) on `session`."""
    return (
        SqlAlchemyCommittedAllocationRepository(session),
        SqlAlchemyDeductionComponentRepository(session),
        SqlAlchemyRevenueFactRepository(session),
        SqlAlchemyChannelAccountLinkRepository(session),
    )


def _commit(committed, ded, rev, link, *, key="k1", fp="fp1", reason="close",
            method="gross_revenue_proportional"):
    return committed.commit_allocation(
        month=MONTH, allocation_method=method, idempotency_key=key,
        request_fingerprint=fp, reason=reason, committed_by=ACTOR,
        deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )


def test_first_commit_creates_version_1(tmp_path):
    """A fresh commit creates run v1 with created=True and persists the line."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    outcome = _commit(committed, ded, rev, link)
    assert outcome.created is True
    assert outcome.run.commit_version == 1
    assert outcome.run.allocated_total_usd == Decimal("100.000000")
    assert len(outcome.lines) == 1
    assert outcome.lines[0].youtube_channel_id == "chA"


def test_idempotent_replay_returns_same_run(tmp_path):
    """Same (month, key, fingerprint) returns the existing run, created=False."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    first = _commit(committed, ded, rev, link, key="dup", fp="same")
    replay = _commit(committed, ded, rev, link, key="dup", fp="same")
    assert replay.created is False
    assert replay.run.id == first.run.id
    assert replay.run.commit_version == 1  # no new version


def test_same_key_different_fingerprint_conflicts(tmp_path):
    """Same (month, key) with a different fingerprint raises a conflict."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    _commit(committed, ded, rev, link, key="dup", fp="fp-a")
    with pytest.raises(CommittedAllocationIdempotencyConflictError):
        _commit(committed, ded, rev, link, key="dup", fp="fp-b")


def test_new_key_same_month_increments_version(tmp_path):
    """A new key in the same month creates the next commit_version."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    _commit(committed, ded, rev, link, key="k1", fp="f1")
    second = _commit(committed, ded, rev, link, key="k2", fp="f2")
    assert second.created is True
    assert second.run.commit_version == 2


def test_locked_month_rejected(tmp_path):
    """Committing a LOCKED month raises CommittedAllocationLockedMonthError."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True, status="LOCKED")
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationLockedMonthError):
        _commit(committed, ded, rev, link)


def test_unsupported_method_rejected_before_compute(tmp_path):
    """A non-gross_revenue_proportional method is rejected (validation error)."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationValidationError):
        _commit(committed, ded, rev, link, method="company_level")


def test_reject_on_unallocated(tmp_path):
    """An unmapped account (no verified channel link) blocks the commit."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=False)
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationValidationError):
        _commit(committed, ded, rev, link)
