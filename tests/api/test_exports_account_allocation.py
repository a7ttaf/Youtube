import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.api.exports import (
    _build_finance_source_summaries_for_export,
    _record_finance_export_artifact_audit,
)
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase
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
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.reports.exports import ExportJobEntry
from ums_smart_revenue.reports.finance_workbook import build_finance_workbook_preview
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    """Create an in-memory SQLite engine with org and finance schemas."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    SecurityBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed_missing_net_with_components(session):
    """A channel with gross but NO source net + a CHANNEL-direct DEDUCTION + an
    ACCOUNT deduction mapped to that channel via a VERIFIED link.
    """
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=TENANT,
            youtube_channel_id="chA",
            channel_name="A",
            active=True,
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            youtube_channel_id="chA",
            source_kind="ADSENSE",
            gross_revenue_usd=Decimal("1000.00"),
            net_revenue_usd=None,
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            component_kind="DEDUCTION",
            scope_kind="CHANNEL",
            scope_id="chA",
            amount_usd=Decimal("30.00"),
            currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            component_key="cd-1",
            raw_payload={},
        )
    )
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(),
            tenant_id=TENANT,
            adsense_account_id="pub-1",
            content_owner_id="owner-1",
            verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED",
            provenance_payload={},
            effective_month_start="2026-01",
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(),
            tenant_id=TENANT,
            content_owner_id="owner-1",
            youtube_channel_id="chA",
            provenance_kind="SOURCE_ROW",
            active=True,
            effective_month_start="2026-01",
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            component_kind="DEDUCTION",
            scope_kind="ACCOUNT",
            scope_id="pub-1",
            amount_usd=Decimal("100.00"),
            currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            component_key="ad-1",
            raw_payload={},
        )
    )
    session.add(
        FinanceMonthCloseORM(
            tenant_id=TENANT, month=MONTH, status="OPEN", allocation_rule_payload={}
        )
    )
    session.commit()


def _export_job(*, scope_type, scope_channel_ids):
    """Build a minimal ExportJobEntry for test scenarios."""
    return ExportJobEntry(
        id="exp-1",
        export_type="FINANCE_EXCEL",
        scope_type=scope_type,
        scope_id=None if scope_type == "global" else "company-a",
        month=MONTH,
        currency="USD",
        requested_by="user-1",
        status="COMPLETED",
        file_url=None,
        month_lock_status="OPEN",
        include_confidence_notes=False,
        include_manual_override_notes=False,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        completed_at=None,
        scope_channel_ids=scope_channel_ids,
    )


def _test_export_user(
    *,
    include_audit: bool = False,
    include_sensitive: bool = False,
) -> UserPrincipal:
    """Build a minimal UserPrincipal for export test scenarios.

    The audit-derived smart-alert signal requires VIEW_AUDIT_LOG, so tests
    opt in explicitly instead of making unrelated export helper coverage depend
    on audit access.
    """
    grants: list[PermissionGrant] = []
    if include_audit:
        grants.append(PermissionGrant(Permission.VIEW_AUDIT_LOG, AccessScope.global_scope()))
    if include_sensitive:
        grants.append(
            PermissionGrant(
                Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS,
                AccessScope.global_scope(),
            )
        )
    return UserPrincipal(
        user_id="user-1",
        email="exp@example.com",
        direct_permissions=tuple(grants),
    )


def _seed_skipped_source_row_audit_edge(session) -> None:
    """Seed a current ROWS_SKIPPED audit edge for the export month."""
    session.add(
        AuditLogORM(
            id=uuid4(),
            tenant_id=TENANT,
            user_id=None,
            event_type="CONNECTOR_JOB_RUN",
            entity_type="connector_run",
            entity_id="run-skipped",
            scope_type="finance-month",
            scope_id=MONTH,
            reason="connector normalize: source rows skipped during projection",
            details={
                "lifecycle": "ROWS_SKIPPED",
                "skipped_count": 4,
                "skipped_by_reason": {"missing_channel_id": 4},
            },
            sensitive=True,
            created_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
    )
    session.commit()


def _seed_out_of_scope_account_allocation(session):
    """A second channel ("chB") with gross but no source net + an ACCOUNT
    deduction mapped to it via a VERIFIED link. It is outside a chA-scoped
    export and must never appear in that export's net-revenue summary.
    """
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=TENANT,
            youtube_channel_id="chB",
            channel_name="B",
            active=True,
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            youtube_channel_id="chB",
            source_kind="ADSENSE",
            gross_revenue_usd=Decimal("500.00"),
            net_revenue_usd=None,
        )
    )
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(),
            tenant_id=TENANT,
            adsense_account_id="pub-2",
            content_owner_id="owner-2",
            verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED",
            provenance_payload={},
            effective_month_start="2026-01",
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(),
            tenant_id=TENANT,
            content_owner_id="owner-2",
            youtube_channel_id="chB",
            provenance_kind="SOURCE_ROW",
            active=True,
            effective_month_start="2026-01",
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            component_kind="DEDUCTION",
            scope_kind="ACCOUNT",
            scope_id="pub-2",
            amount_usd=Decimal("70.00"),
            currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            component_key="ad-2",
            raw_payload={},
        )
    )
    session.commit()


def test_scoped_export_excludes_out_of_scope_account_allocation(tmp_path):
    """Regression (PR #59 review): a company-scoped export froze channel_ids to
    ("chA",), but the month-wide account allocation also produced a line for the
    out-of-scope "chB". The scoped export must not leak chB's allocation channel
    or totals into the exported net-revenue summary.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _seed_out_of_scope_account_allocation(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="company", scope_channel_ids=("chA",)),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    channel_ids = {c.youtube_channel_id for c in summaries.net_revenue.channels}
    assert channel_ids == {"chA"}  # chB allocation line filtered out
    # chA's own allocation still applies; chB's 70.00 never lands in the totals.
    channel_a = summaries.net_revenue.channels[0]
    assert channel_a.account_allocated_deduction_amount_usd == Decimal("100.000000")


def test_export_net_reflects_channel_direct_and_account_deductions(tmp_path):
    """Regression: exports previously passed NO deduction_components, so export net
    diverged from API net. Now the export source summary nets out BOTH the
    channel-direct (30) and the account-allocated (100) deductions.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    channel = summaries.net_revenue.channels[0]
    assert channel.status == "COMPONENT_DERIVED"
    assert channel.net_revenue_usd == Decimal("870.000000")  # 1000 - 30 - 100
    assert channel.channel_direct_deduction_amount_usd == Decimal("30.00")
    assert channel.account_allocated_deduction_amount_usd == Decimal("100.000000")


def test_global_export_preview_includes_skipped_source_alert_for_audit_user(tmp_path):
    """Global preview can surface audit-derived skipped source rows to audit users."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _seed_skipped_source_row_audit_edge(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(include_audit=True),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )

    skipped = next(
        (alert for alert in summaries.smart_alerts.alerts if alert.code == "SOURCE_ROWS_SKIPPED"),
        None,
    )
    assert skipped is not None
    assert skipped.details == {
        "skipped_count": 4,
        "skipped_by_reason": {},
    }


def test_scoped_export_suppresses_tenant_wide_skipped_source_alert(tmp_path):
    """Scoped exports must not leak tenant-wide skipped-row audit signals."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _seed_skipped_source_row_audit_edge(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="company", scope_channel_ids=("chA",)),
            user=_test_export_user(include_audit=True),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )

    assert "SOURCE_ROWS_SKIPPED" not in {alert.code for alert in summaries.smart_alerts.alerts}


def test_persisted_artifact_source_summary_is_permission_invariant(tmp_path):
    """Persisted download builders suppress caller-specific audit-derived alerts."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _seed_skipped_source_row_audit_edge(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(include_audit=True, include_sensitive=True),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
            include_audit_derived_alerts=False,
        )

    assert "SOURCE_ROWS_SKIPPED" not in {alert.code for alert in summaries.smart_alerts.alerts}


def _commit_snapshot_and_lock(session):
    """Commit one account-allocation snapshot for MONTH, then lock the month.

    The seed leaves the close row OPEN so the commit succeeds; the lock is applied
    afterwards so the read-switch resolver prefers the committed snapshot.
    """
    committed = SqlAlchemyCommittedAllocationRepository(session)
    committed.commit_allocation(
        month=MONTH,
        allocation_method="gross_revenue_proportional",
        idempotency_key="k1",
        request_fingerprint="fp1",
        reason="close",
        committed_by=str(TENANT),
        deduction_repository=SqlAlchemyDeductionComponentRepository(session),
        revenue_repository=SqlAlchemyRevenueFactRepository(session),
        link_repository=SqlAlchemyChannelAccountLinkRepository(session),
    )
    close = session.query(FinanceMonthCloseORM).filter_by(tenant_id=TENANT, month=MONTH).one()
    close.status = "LOCKED"
    session.commit()


def test_export_bundle_locked_month_uses_committed_snapshot_provenance(tmp_path):
    """A LOCKED month with a committed run -> the export bundle carries
    account_allocation_provenance.source == "committed_snapshot".
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _commit_snapshot_and_lock(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    assert summaries.account_allocation_provenance.source == "committed_snapshot"
    assert summaries.account_allocation_provenance.commit_version == 1


def test_export_bundle_open_month_uses_live_compute_provenance(tmp_path):
    """An OPEN month -> the export bundle reports live_compute (no committed run used)."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    assert summaries.account_allocation_provenance.source == "live_compute"
    assert summaries.account_allocation_provenance.commit_version is None


def test_workbook_preview_payload_carries_allocation_provenance(tmp_path):
    """The finance workbook preview to_api() surfaces the allocation provenance in
    its source_summaries map for a committed-snapshot LOCKED month.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _commit_snapshot_and_lock(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    preview = build_finance_workbook_preview(
        export_job=_export_job(scope_type="global", scope_channel_ids=None),
        net_revenue=summaries.net_revenue,
        payment_match=summaries.payment_match,
        bank_reconciliation=summaries.bank_reconciliation,
        smart_alerts=summaries.smart_alerts,
        account_allocation_provenance=summaries.account_allocation_provenance,
    )
    provenance_api = preview.to_api()["source_summaries"]["account_allocation_provenance"]
    assert provenance_api["allocation_source"] == "committed_snapshot"
    assert provenance_api["committed_run"]["commit_version"] == 1


def _commit_post_tax_snapshot_and_lock(session):
    """Set chA's source net non-null, commit a post_tax snapshot, then lock.

    post_tax_revenue_proportional weights the split by source net, so the seed's
    null net is replaced first (else the commit fails closed on missing net). The
    lock is applied afterwards so the read-switch resolver prefers the snapshot.
    """
    session.execute(
        MonthlyChannelRevenueFactORM.__table__.update()
        .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
        .values(net_revenue_usd=Decimal("800.00"))
    )
    session.commit()
    committed = SqlAlchemyCommittedAllocationRepository(session)
    committed.commit_allocation(
        month=MONTH,
        allocation_method="post_tax_revenue_proportional",
        idempotency_key="k-pt",
        request_fingerprint="fp-pt",
        reason="close",
        committed_by=str(TENANT),
        deduction_repository=SqlAlchemyDeductionComponentRepository(session),
        revenue_repository=SqlAlchemyRevenueFactRepository(session),
        link_repository=SqlAlchemyChannelAccountLinkRepository(session),
    )
    close = session.query(FinanceMonthCloseORM).filter_by(tenant_id=TENANT, month=MONTH).one()
    close.status = "LOCKED"
    session.commit()


def test_export_committed_post_tax_month_renders_only_disclosure_token(tmp_path):
    """Rename regression: an export over a LOCKED, committed post_tax month carries
    only the allocation-source disclosure token in its payload; no per-line basis
    field (basis_amount_usd / the old basis_gross_usd) ever leaks into the export.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        _commit_post_tax_snapshot_and_lock(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    # The committed post_tax snapshot is the resolved allocation source.
    assert summaries.account_allocation_provenance.source == "committed_snapshot"
    preview = build_finance_workbook_preview(
        export_job=_export_job(scope_type="global", scope_channel_ids=None),
        net_revenue=summaries.net_revenue,
        payment_match=summaries.payment_match,
        bank_reconciliation=summaries.bank_reconciliation,
        smart_alerts=summaries.smart_alerts,
        account_allocation_provenance=summaries.account_allocation_provenance,
    )
    payload = preview.to_api()
    provenance_api = payload["source_summaries"]["account_allocation_provenance"]
    # Only the allocation-source disclosure token is exposed (no committed run
    # internals beyond the version stamp; no per-line basis whatsoever).
    assert provenance_api["allocation_source"] == "committed_snapshot"
    # No per-line basis field leaks into the full serialized export payload.
    serialized = json.dumps(payload, default=str)
    assert "basis_amount_usd" not in serialized
    assert "basis_gross_usd" not in serialized


def test_scoped_finance_export_records_payment_viewed():
    """A scoped (company) finance-artifact export now emits PAYMENT_VIEWED (was
    global-only); BANK_RECONCILIATION_VIEWED stays global-only.
    """
    sink = InMemoryAuditSink()
    user = UserPrincipal(user_id="user-1", email="exp@example.com")
    records = _record_finance_export_artifact_audit(
        audit_sink=sink,
        user=user,
        export_job=_export_job(scope_type="company", scope_channel_ids=("chA",)),
        group_registry=ChannelGroupRegistry(),
        artifact_type="finance_workbook_xlsx",
        include_download_event=False,
    )
    kinds = {record.event_type for record in records}
    assert "REVENUE_VIEWED" in kinds
    assert "PAYMENT_VIEWED" in kinds
    assert "BANK_RECONCILIATION_VIEWED" not in kinds  # scoped: no bank exposure


def test_global_finance_export_still_records_bank_reconciliation_viewed():
    """Global finance export keeps PAYMENT_VIEWED + BANK_RECONCILIATION_VIEWED."""
    sink = InMemoryAuditSink()
    user = UserPrincipal(user_id="user-1", email="exp@example.com")
    records = _record_finance_export_artifact_audit(
        audit_sink=sink,
        user=user,
        export_job=_export_job(scope_type="global", scope_channel_ids=None),
        group_registry=ChannelGroupRegistry(),
        artifact_type="finance_workbook_xlsx",
        include_download_event=False,
    )
    kinds = {record.event_type for record in records}
    assert {"REVENUE_VIEWED", "PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED"} <= kinds


def test_export_bundle_includes_coverage_alert_for_factless_channels(tmp_path):
    """Export finance bundle surfaces CHANNELS_MISSING_REVENUE_FACTS (review #98 T9).

    A 2nd active revenue_required channel with no fact for the month must show
    up in the smart-alerts summary that the export bundle carries, so the
    exported workbook discloses the same coverage gap the API surfaces.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        # Add a 2nd active, revenue_required channel with no fact for the month.
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT,
                youtube_channel_id="chB",
                channel_name="B",
                active=True,
                revenue_required=True,
            )
        )
        session.commit()
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    coverage_codes = [alert.code for alert in summaries.smart_alerts.alerts]
    assert "CHANNELS_MISSING_REVENUE_FACTS" in coverage_codes
    coverage = next(
        (
            alert
            for alert in summaries.smart_alerts.alerts
            if alert.code == "CHANNELS_MISSING_REVENUE_FACTS"
        ),
        None,
    )
    assert coverage is not None
    assert coverage.details["channel_count"] == 1
    assert coverage.details["sample_channel_ids"] == ["chB"]


def test_export_bundle_coverage_alert_respects_frozen_channel_scope(tmp_path):
    """Scoped export's CHANNELS_MISSING_REVENUE_FACTS sample
    is restricted to channel_ids (review #98 T13).

    A company/sector/group export must never include channel ids outside its
    frozen channel set, even when those channels are factless. The export
    helper now passes `youtube_channel_ids=channel_ids` so the
    missing-revenue-fact read is restricted to the export's scope. Global
    exports pass channel_ids=None and keep the tenant-global view.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        # Add a 2nd active, revenue_required channel with no fact for the month.
        # This channel is OUTSIDE the export's frozen channel set.
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT,
                youtube_channel_id="ch-outside",
                channel_name="Outside",
                active=True,
                revenue_required=True,
            )
        )
        session.commit()
        # Scoped export: frozen channel_ids = ("chA",) only.
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="company", scope_channel_ids=("chA",)),
            user=_test_export_user(),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    coverage = next(
        (
            alert
            for alert in summaries.smart_alerts.alerts
            if alert.code == "CHANNELS_MISSING_REVENUE_FACTS"
        ),
        None,
    )
    # The scoped export's channel set has no factless channel, so the
    # coverage alert is absent (or zero-count) for the scoped view.
    if coverage is not None:
        assert coverage.details["channel_count"] == 0
        assert "ch-outside" not in coverage.details["sample_channel_ids"]
    # And the global export (no frozen channel set) still surfaces it.
    global_summaries = _build_finance_source_summaries_for_export(
        export_job=_export_job(scope_type="global", scope_channel_ids=None),
        user=_test_export_user(),
        session=session,
        org_index=OrgAccessIndex(),
        group_registry=ChannelGroupRegistry(),
    )
    global_coverage = next(
        (
            alert
            for alert in global_summaries.smart_alerts.alerts
            if alert.code == "CHANNELS_MISSING_REVENUE_FACTS"
        ),
        None,
    )
    assert global_coverage is not None
    assert global_coverage.details["channel_count"] == 1
    assert "ch-outside" in global_coverage.details["sample_channel_ids"]
