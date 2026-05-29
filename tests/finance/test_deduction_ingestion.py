"""Repository + service tests for deduction-component ingestion (SQLite).

Test suite for deduction ingestion.

This module sets up fixtures to import the deduction ingestion module,
create a UserPrincipal actor, initialize a SQLite engine, and seed test data
for finance-related tests.
"""
from datetime import date
from decimal import Decimal
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM

MONTH = "2026-04"
ACTOR_ID = UUID("00000000-0000-0000-0000-0000000c0001")


def _mod():
    """Import and return the deduction_ingestion module from ums_smart_revenue.finance."""
    return import_module("ums_smart_revenue.finance.deduction_ingestion")


def _actor() -> UserPrincipal:
    """Create and return a UserPrincipal representing the finance viewer actor."""
    return UserPrincipal(
        user_id=str(ACTOR_ID),
        email="ingest@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )


def _engine(tmp_path):
    """
    Create a SQLite engine in a temporary file path, initialize finance tables, and return the engine.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    )
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed(session: Session, *, settled="1000.00", paid="930.00", tax_currency="USD",
          locked=False):
    """
    Seed the database session with test data: BankReconciliationEntry, AdSensePayment, and GoogleRevenueSourceRow entries.
    """
    session.add(
        BankReconciliationEntryORM(
            id=uuid4(), month=MONTH, bank_reference="BANK-1",
            bank_received_date=date(2026, 4, 20),
            bank_received_amount=Decimal("1000.00"), bank_received_currency="USD",
            bank_received_amount_usd=Decimal("1000.00"),
            transfer_fee_usd=Decimal("3.50"), fx_difference_usd=Decimal("-2.00"),
            recorded_by=ACTOR_ID,
        )
    )
    session.add(
        AdSensePaymentORM(
            id=uuid4(), month=MONTH, payment_name="apr", source_account_id="pub-1",
            payment_date=date(2026, 5, 21), payment_amount=Decimal(paid),
            payment_currency="USD", payment_status="PAID", raw_payload={},
            source_report_id=None, imported_by=ACTOR_ID,
        )
    )
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=_ums_tenant(), source_system="adsense_management",
            source_row_key=("settled-key").ljust(64, "0")[:64],
            source_account_id="pub-1", youtube_channel_id=None, report_type="r",
            report_month=MONTH, period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30), metric_key="m", value_kind="settled",
            amount_native=Decimal(settled), currency_code="USD",
            source_report_id=None, raw_payload={},
        )
    )
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=_ums_tenant(), source_system="adsense_management",
            source_row_key=("tax-key").ljust(64, "0")[:64],
            source_account_id="pub-1", youtube_channel_id=None, report_type="r",
            report_month=MONTH, period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30), metric_key="m", value_kind="tax",
            amount_native=Decimal("11.00"), currency_code=tax_currency,
            source_report_id=None, raw_payload={},
        )
    )
    if locked:
        session.add(
            FinanceMonthCloseORM(
                tenant_id=_ums_tenant(), month=MONTH, status="LOCKED",
                allocation_rule_payload={},
            )
        )
    session.commit()


def _ums_tenant() -> UUID:
    """Return the UUID for the UMS tenant from the tenancy constants."""
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
    return UUID(UMS_TENANT_ID)


def _service(session):
    """Initialize and return the DeductionIngestionService with an in-memory audit sink for testing."""
    return _mod().DeductionIngestionService(session, audit_sink=InMemoryAuditSink())


def test_ingest_creates_components_from_all_sources(tmp_path):
    """Test that ingest creates deduction components for all expected source kinds and records an audit event."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        result = service.ingest(month=MONTH, actor=_actor(), reason="monthly")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        kinds = {c.component_kind for c in repo.list_month_components(month=MONTH)}
    assert {"TRANSFER_FEE", "FX_VARIANCE", "UNRESOLVED_PAYMENT_GAP", "TAX"} <= kinds
    assert result.by_kind["UNRESOLVED_PAYMENT_GAP"] == 1
    assert result.total_upserted >= 4
    assert sink.records[-1].event_type == "DEDUCTION_COMPONENTS_INGESTED"


def test_ingest_is_idempotent(tmp_path):
    """Test that repeated ingestion for the same month does not create duplicate components."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        service = _service(session)
        service.ingest(month=MONTH, actor=_actor(), reason="r1")
        session.commit()
        service.ingest(month=MONTH, actor=_actor(), reason="r2")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        components = repo.list_month_components(month=MONTH)
    keys = [c.component_key for c in components]
    assert len(keys) == len(set(keys))  # no duplicates after re-ingest


def test_ingest_refuses_locked_month(tmp_path):
    """Test that ingesting a locked month raises DeductionComponentLockedMonthError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session, locked=True)
        service = _service(session)
        with pytest.raises(_mod().DeductionComponentLockedMonthError):
            service.ingest(month=MONTH, actor=_actor(), reason="r")


def test_ingest_refuses_locked_month_even_with_zero_components(tmp_path):
    """Test that a locked month with no source evidence still raises an error and writes no audit records."""
    # Lock the month but seed NO source evidence -> zero mapped components. Live
    # ingestion must still fail closed (no audit write) — the lock check must
    # precede the empty-component short-circuit in upsert_components.
    engine = _engine(tmp_path)
    with Session(engine) as session:
        from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
        session.add(
            FinanceMonthCloseORM(
                tenant_id=UUID(UMS_TENANT_ID), month=MONTH, status="LOCKED",
                allocation_rule_payload={},
            )
        )
        session.commit()
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        with pytest.raises(_mod().DeductionComponentLockedMonthError):
            service.ingest(month=MONTH, actor=_actor(), reason="r")
        assert sink.records == []  # no audit written on a refused locked run


def test_ingest_skips_non_usd_and_counts_it(tmp_path):
    """Test that non-USD tax rows are skipped and counted in the skipped_non_usd result."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session, tax_currency="EUR")
        service = _service(session)
        result = service.ingest(month=MONTH, actor=_actor(), reason="r")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        kinds = [c.component_kind for c in repo.list_month_components(month=MONTH)]
    assert result.skipped_non_usd >= 1
    assert "TAX" not in kinds  # the EUR tax row was skipped, not stored


def test_dry_run_writes_nothing_and_records_no_audit(tmp_path):
    """Test that dry run mode does not persist components or write audit records, but reports potential upserts."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        result = service.ingest(month=MONTH, actor=_actor(), reason="r", dry_run=True)
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        components = repo.list_month_components(month=MONTH)
    assert components == []
    assert sink.records == []
    assert result.total_upserted >= 4  # would-upsert count is still reported


def test_audit_details_carry_only_summary_counts(tmp_path):
    """Test that the audit details record only summary counts and no sensitive payload data."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        service.ingest(month=MONTH, actor=_actor(), reason="r")
        session.commit()
    details = sink.records[-1].details
    assert set(details) == {"month", "total_upserted", "by_kind", "skipped_non_usd"}
    # No amounts/currencies/payloads leak into the audit record.
    assert "amount" not in repr(details).lower()
