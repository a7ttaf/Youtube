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
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import OrgAccessIndex
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.reports.exports import ExportJobEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed_missing_net_with_components(session):
    """A channel with gross but NO source net + a CHANNEL-direct DEDUCTION + an
    ACCOUNT deduction mapped to that channel via a VERIFIED link."""
    session.add(
        YouTubeChannelORM(
            id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
            channel_name="A", active=True,
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
            source_kind="ADSENSE", gross_revenue_usd=Decimal("1000.00"),
            net_revenue_usd=None,
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
            scope_kind="CHANNEL", scope_id="chA", amount_usd=Decimal("30.00"),
            currency_code="USD", source_system="adsense_management",
            source_table="google_revenue_source_rows", component_key="cd-1",
            raw_payload={},
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
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW", active=True,
            effective_month_start="2026-01",
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
            scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
            currency_code="USD", source_system="adsense_management",
            source_table="google_revenue_source_rows", component_key="ad-1",
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
    return ExportJobEntry(
        id="exp-1", export_type="FINANCE_EXCEL", scope_type=scope_type,
        scope_id=None if scope_type == "global" else "company-a", month=MONTH,
        currency="USD", requested_by="user-1", status="COMPLETED", file_url=None,
        month_lock_status="OPEN", include_confidence_notes=False,
        include_manual_override_notes=False,
        created_at=datetime(2026, 4, 1, tzinfo=UTC), completed_at=None,
        scope_channel_ids=scope_channel_ids,
    )


def test_export_net_reflects_channel_direct_and_account_deductions(tmp_path):
    """Regression: exports previously passed NO deduction_components, so export net
    diverged from API net. Now the export source summary nets out BOTH the
    channel-direct (30) and the account-allocated (100) deductions."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    channel = summaries.net_revenue.channels[0]
    assert channel.status == "COMPONENT_DERIVED"
    assert channel.net_revenue_usd == Decimal("870.000000")  # 1000 - 30 - 100
    assert channel.channel_direct_deduction_amount_usd == Decimal("30.00")
    assert channel.account_allocated_deduction_amount_usd == Decimal("100.000000")


def test_scoped_finance_export_records_payment_viewed():
    """A scoped (company) finance-artifact export now emits PAYMENT_VIEWED (was
    global-only); BANK_RECONCILIATION_VIEWED stays global-only."""
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
