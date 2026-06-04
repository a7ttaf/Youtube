"""Read-switch resolver: lock-aware snapshot-vs-live selection + reconstruction."""
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    CommittedAllocationRunORM,
    CommittedAllocationUnallocatedORM,
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
    rebuild_result_from_run,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommitAllocationOutcome,
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
    """Create a single-use in-memory SQLite session with FK enforcement for one test."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        """Enable SQLite FK enforcement on every new connection."""
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


def _add_channel(session, *, channel, gross, source_kind="ADSENSE"):
    """Seed one channel + its source-aligned monthly gross fact."""
    session.add(YouTubeChannelORM(
        id=uuid4(), tenant_id=TENANT, youtube_channel_id=channel, channel_name=channel, active=True,
    ))
    session.flush()  # cross-registry composite FK: channel before the dependent fact
    session.add(MonthlyChannelRevenueFactORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id=channel,
        source_kind=source_kind, gross_revenue_usd=Decimal(gross), net_revenue_usd=None,
    ))


def _map_account_to_channels(session, *, account, channels, owner=None):
    """Verify-map one ACCOUNT to a set of already-seeded channels (one owner)."""
    owner = owner or f"owner-{account}"
    session.add(AdsenseContentOwnerLinkORM(
        id=uuid4(), tenant_id=TENANT, adsense_account_id=account, content_owner_id=owner,
        verification_status="VERIFIED", provenance_kind="OPERATOR_ASSERTED",
        provenance_payload={}, effective_month_start="2026-01",
    ))
    for channel in channels:
        session.add(ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id=owner, youtube_channel_id=channel,
            provenance_kind="SOURCE_ROW", active=True, effective_month_start="2026-01",
        ))


def _add_account_deduction(session, *, account, deduction, component_key=None,
                           source_system="adsense_management"):
    """Seed one ACCOUNT-scoped deduction component (no channel/map seeding)."""
    session.add(DeductionComponentORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
        scope_kind="ACCOUNT", scope_id=account, amount_usd=Decimal(deduction),
        currency_code="USD", source_system=source_system,
        source_table="google_revenue_source_rows",
        component_key=component_key or f"ad-{account}", raw_payload={},
    ))


def _close(session, status):
    """Set the finance-month close status, updating the existing row if already created."""
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
    """Return (committed, deduction, revenue, link) repositories bound to session."""
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
    """Call resolve_month_account_allocation for MONTH using all repos from session."""
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
    _committed, ded, rev, link = _repos(session)
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


def test_per_account_filter_actively_drops_cross_account_note(tmp_path):
    """A channel reachable from TWO accounts persists a CHANNEL_IN_MULTIPLE_ACCOUNTS
    note in the month-wide snapshot; the per-account filter ACTIVELY drops it.

    Genuinely exercises filter_committed_result_to_account's notes=() drop (not the
    tautological both-sides-empty case): the snapshot demonstrably HOLDS a note, the
    scoped resolve returns (), and live single-account compute also returns () so
    snapshot/live equivalence is preserved.
    """
    session = _session(tmp_path)
    # chShared is verified-mapped to BOTH pub-1 and pub-2; each account also has its
    # own ACCOUNT deduction, so both accounts appear in verified_channels and the
    # month-wide compute emits one CHANNEL_IN_MULTIPLE_ACCOUNTS note for chShared.
    _add_channel(session, channel="chShared", gross="1000.00")
    _map_account_to_channels(session, account="pub-1", channels=["chShared"], owner="owner-1")
    _map_account_to_channels(session, account="pub-2", channels=["chShared"], owner="owner-2")
    _add_account_deduction(session, account="pub-1", deduction="100.00")
    _add_account_deduction(session, account="pub-2", deduction="40.00")
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    _close(session, "LOCKED")

    # (a) the month-wide snapshot actually holds a note to drop.
    outcome = committed.get_latest_committed(MONTH)
    raw = rebuild_result_from_run(outcome)
    assert any(n.note_code == "CHANNEL_IN_MULTIPLE_ACCOUNTS" for n in raw.notes)
    assert any(n.youtube_channel_id == "chShared" for n in raw.notes)

    # (b) resolve scoped to one account -> committed_snapshot with the note dropped.
    snap, prov = _resolve(session, adsense_account_id="pub-1")
    assert prov.source == "committed_snapshot"
    assert snap.notes == ()

    # (c) live single-account compute for that account also has no note (equivalence).
    live = compute_month_account_allocation(
        month=MONTH, deduction_repository=ded, revenue_repository=rev,
        link_repository=link, adsense_account_id="pub-1",
    )
    assert live.notes == ()
    assert snap.lines == live.lines
    assert snap.summary == live.summary


def _commit_multi(session):
    """Seed 2 accounts, each verified-mapped to 2 distinct channels, and commit."""
    _add_channel(session, channel="chA1", gross="600.00")
    _add_channel(session, channel="chA2", gross="400.00")
    _add_channel(session, channel="chB1", gross="300.00")
    _add_channel(session, channel="chB2", gross="100.00")
    _map_account_to_channels(session, account="pub-1", channels=["chA1", "chA2"], owner="owner-1")
    _map_account_to_channels(session, account="pub-2", channels=["chB1", "chB2"], owner="owner-2")
    _add_account_deduction(session, account="pub-1", deduction="100.00")
    _add_account_deduction(session, account="pub-2", deduction="40.00")
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    _close(session, "LOCKED")


def _line_sort_key(line):
    """Stable cross-implementation sort key for an AllocationLine."""
    return (line.adsense_account_id, line.youtube_channel_id, line.component_key)


def test_reconstruction_multi_account_matches_live_sorted_and_is_deterministic(tmp_path):
    """LOCKED reconstruction over ≥2 accounts × ≥2 channels equals live as SORTED
    sequences, and two successive committed reads return lines in identical order.

    Asserts the deterministic ORDER BY contract WITHOUT over-constraining to live's
    incidental emission order: both sides are sorted by (account, channel, key).
    """
    session = _session(tmp_path)
    _commit_multi(session)
    committed, ded, rev, link = _repos(session)

    snap, prov = _resolve(session)
    assert prov.source == "committed_snapshot"
    live = compute_month_account_allocation(
        month=MONTH, deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )
    # equal as sets/sorted sequences (don't assume live's emission order)
    assert sorted(snap.lines, key=_line_sort_key) == sorted(live.lines, key=_line_sort_key)
    assert snap.summary == live.summary

    # determinism: two successive reconstructed reads emit lines in the SAME order.
    first = rebuild_result_from_run(committed.get_latest_committed(MONTH)).lines
    second = rebuild_result_from_run(committed.get_latest_committed(MONTH)).lines
    assert first == second
    # ...and the order is the deterministic ORDER BY (account, channel, key).
    assert list(first) == sorted(first, key=_line_sort_key)


def test_zero_amount_component_yields_no_line_and_row_derived_counts(tmp_path):
    """A zero-amount ACCOUNT component is a counted no-op in live compute, but the
    snapshot persists NO line for it; the per-account filter recomputes counts from
    rows, so component_count/allocated_component_count are 0 for that account.

    Pins the DOCUMENTED edge in filter_committed_result_to_account: monetary totals
    stay exact (0) while counts are row-derived. Verified against live single-account
    compute, which (unlike the row-derived snapshot filter) counts the no-op.
    """
    session = _session(tmp_path)
    _add_channel(session, channel="chZ", gross="1000.00")
    _map_account_to_channels(session, account="pub-z", channels=["chZ"], owner="owner-z")
    _add_account_deduction(session, account="pub-z", deduction="0")  # zero-amount no-op
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    _close(session, "LOCKED")

    snap, prov = _resolve(session, adsense_account_id="pub-z")
    assert prov.source == "committed_snapshot"
    # zero-amount component produces NO line (allocate-component no-op path).
    assert snap.lines == ()
    # monetary totals exact (0).
    assert snap.summary.allocated_total_usd == Decimal("0")
    assert snap.summary.net_applicable_total_usd == Decimal("0")
    # counts are row-derived in the snapshot filter: no rows -> 0.
    assert snap.summary.component_count == 0
    assert snap.summary.allocated_component_count == 0

    # Live single-account compute counts the no-op (it sees the component, not rows);
    # this is the documented divergence between the live count and the row-derived
    # snapshot filter. Monetary totals agree exactly.
    live = compute_month_account_allocation(
        month=MONTH, deduction_repository=ded, revenue_repository=rev,
        link_repository=link, adsense_account_id="pub-z",
    )
    assert live.lines == ()
    assert live.summary.allocated_total_usd == Decimal("0")
    assert live.summary.component_count == 1
    assert live.summary.allocated_component_count == 1


def test_rebuild_maps_unallocated_fields_from_a_synthetic_run():
    """rebuild_result_from_run maps every UnallocatedIssue field from a child row.

    Constructs a CommitAllocationOutcome with a synthetic unallocated child directly
    (the write path rejects on unallocated, so this read mapping is otherwise never
    exercised with real data). No session/DB needed — pure mapping.
    """
    run = CommittedAllocationRunORM(
        id=UUID(int=7), tenant_id=TENANT, month=MONTH, commit_version=1,
        allocation_method="gross_revenue_proportional",
        idempotency_key="k", request_fingerprint="fp",
        component_count=1, allocated_component_count=0, unallocated_component_count=1,
        allocated_total_usd=Decimal("0"), unallocated_total_usd=Decimal("9.50"),
        net_applicable_total_usd=Decimal("0"), reconciliation_total_usd=Decimal("0"),
        committed_by=UUID(int=1), reason="r",
    )
    issue_row = CommittedAllocationUnallocatedORM(
        id=UUID(int=8), run_id=run.id, scope_id="pub-x", component_kind="DEDUCTION",
        component_key="acct-ded-x", amount_usd=Decimal("9.50"),
        issue_code="ACCOUNT_UNMAPPED_OR_UNVERIFIED", detail="no verified channels",
    )
    outcome = CommitAllocationOutcome(
        run=run, lines=(), unallocated=(issue_row,), notes=(), created=False,
    )
    result = rebuild_result_from_run(outcome)
    assert len(result.unallocated) == 1
    iss = result.unallocated[0]
    assert iss.scope_id == "pub-x"
    assert iss.component_kind == "DEDUCTION"
    assert iss.component_key == "acct-ded-x"
    assert iss.amount_usd == Decimal("9.50")
    assert iss.issue_code == "ACCOUNT_UNMAPPED_OR_UNVERIFIED"
    assert iss.detail == "no verified channels"
    assert result.summary.unallocated_total_usd == Decimal("9.50")


def test_resolve_serves_committed_post_tax_snapshot_for_locked_month(tmp_path):
    """LOCKED month -> committed post_tax snapshot (reconstructed losslessly)."""
    session = _session(tmp_path)
    _add_account(
        session, account="pub-1", channel="chA",
        gross="1000.00", deduction="100.00", mapped=True,
    )
    session.execute(
        MonthlyChannelRevenueFactORM.__table__.update()
        .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
        .values(net_revenue_usd=Decimal("800.00"))
    )
    session.commit()
    committed = SqlAlchemyCommittedAllocationRepository(session)
    ded = SqlAlchemyDeductionComponentRepository(session)
    rev = SqlAlchemyRevenueFactRepository(session)
    link = SqlAlchemyChannelAccountLinkRepository(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="post_tax_revenue_proportional",
        idempotency_key="k-pt", request_fingerprint="fp-pt", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    # commit_allocation auto-created the OPEN close row; lock it so the
    # read-switch prefers the committed snapshot.
    close = session.scalars(
        select(FinanceMonthCloseORM).where(
            FinanceMonthCloseORM.tenant_id == TENANT,
            FinanceMonthCloseORM.month == MONTH,
        )
    ).one()
    close.status = "LOCKED"
    session.commit()
    result, provenance = resolve_month_account_allocation(
        month=MONTH, session=session, deduction_repository=ded,
        revenue_repository=rev, link_repository=link, committed_repository=committed,
    )
    assert provenance.source == "committed_snapshot"
    assert result.allocation_method == "post_tax_revenue_proportional"
    assert result.lines[0].basis_amount_usd == Decimal("800.000000")


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
