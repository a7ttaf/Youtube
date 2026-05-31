"""Repository + service tests for deduction-component ingestion (SQLite)."""
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

MONTH = "2026-04"
ACTOR_ID = UUID("00000000-0000-0000-0000-0000000c0001")


def _mod():
    """Import the deduction ingestion module."""
    return import_module("ums_smart_revenue.finance.deduction_ingestion")


def _actor() -> UserPrincipal:
    """Create the finance viewer actor used by service tests."""
    return UserPrincipal(
        user_id=str(ACTOR_ID),
        email="ingest@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )


def _engine(tmp_path):
    """Create a temporary SQLite finance schema."""
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    )
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed(session: Session, *, settled="1000.00", paid="930.00", tax_currency="USD",
          locked=False):
    """Seed source rows, bank entries, payments, and optional month lock."""
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


def _component(component_key: str, *, amount: str = "1.00"):
    """Build a repository component input with a stable idempotency key."""
    return _mod().DeductionComponentInput(
        component_kind="TRANSFER_FEE",
        scope_kind="PAYMENT",
        scope_id=f"bank-{component_key}",
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system="bank_reconciliation",
        source_table="bank_reconciliation_entries",
        source_id=None,
        source_key=component_key,
        source_report_id=None,
        raw_payload={},
        component_key=component_key,
    )


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


def test_ingest_requires_non_blank_reason_before_writes(tmp_path):
    """Service/API callers must provide the required audit reason."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        with pytest.raises(_mod().DeductionComponentValidationError):
            service.ingest(month=MONTH, actor=_actor(), reason="  ")
        session.rollback()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        assert repo.list_month_components(month=MONTH) == []
        assert sink.records == []


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


def test_repository_reingest_removes_stale_month_components(tmp_path):
    """Live re-ingestion removes tenant/month components no longer produced."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        repo.upsert_components(
            month=MONTH,
            components=[_component("bank-a"), _component("bank-b")],
        )
        session.commit()

        repo.upsert_components(
            month=MONTH,
            components=[_component("bank-a", amount="2.00")],
        )
        session.commit()

        components = repo.list_month_components(month=MONTH)
        assert [(c.component_key, c.amount_usd) for c in components] == [
            ("bank-a", Decimal("2.00"))
        ]

        repo.upsert_components(month=MONTH, components=[])
        session.commit()

        assert repo.list_month_components(month=MONTH) == []


def test_scoped_bank_ingest_preserves_other_source_components(tmp_path):
    """A bank-only rerun removes stale bank rows, not other source families."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        service = _service(session)
        service.ingest(month=MONTH, actor=_actor(), reason="full")
        session.commit()

        bank_entry = session.scalars(select(BankReconciliationEntryORM)).one()
        bank_entry.fx_difference_usd = Decimal("0.00")
        session.commit()

        service.ingest(month=MONTH, actor=_actor(), reason="bank", source="bank")
        session.commit()

        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        component_keys = {
            component.component_key
            for component in repo.list_month_components(month=MONTH)
        }

    assert "bank:2026-04:BANK-1:transfer_fee" in component_keys
    assert "bank:2026-04:BANK-1:fx_variance" not in component_keys
    assert any(key.startswith("srcrow:") for key in component_keys)
    assert "adsense_gap:pub-1:2026-04" in component_keys


def test_repository_uses_request_tenant_context_by_default(tmp_path):
    """Ambient TENANT_CTX scopes deduction component writes by default."""
    engine = _engine(tmp_path)
    tenant_id = UUID("00000000-0000-0000-0000-000000042001")
    now = datetime(2026, 4, 1, tzinfo=UTC)
    token = TENANT_CTX.set(
        Tenant(
            id=tenant_id,
            slug="ctx",
            display_name="Context Tenant",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    try:
        with Session(engine) as session:
            repo = _mod().SqlAlchemyDeductionComponentRepository(session)
            repo.upsert_components(month=MONTH, components=[_component("ctx-bank")])
            session.commit()
            row = session.scalars(select(DeductionComponentORM)).one()
    finally:
        TENANT_CTX.reset(token)

    assert row.tenant_id == tenant_id


def test_repository_rejects_non_ascii_month_digits(tmp_path):
    """Unicode decimal digits must not satisfy the YYYY-MM month contract."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(_mod().DeductionComponentValidationError):
            repo.list_month_components(month="٢٠٢٦-٠٤")


def test_dry_run_refuses_locked_month(tmp_path):
    """Test that dry-run mode rejects locked finance months without audit writes."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session, locked=True)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        with pytest.raises(_mod().DeductionComponentLockedMonthError):
            service.ingest(month=MONTH, actor=_actor(), reason="r", dry_run=True)
        assert sink.records == []


@pytest.mark.parametrize(
    ("selected_source", "expected_reads"),
    [
        ("source_rows", {"source_rows"}),
        ("bank", {"bank"}),
        ("gap", {"source_rows", "payments"}),
    ],
)
def test_source_filter_reads_only_required_adapters(
    monkeypatch, selected_source, expected_reads
):
    """Test that a source-filtered dry run does not touch unrelated adapters."""
    module = _mod()
    reads: list[str] = []

    class _FakeRepository:
        """Repository stub that lets dry-run lock validation pass."""

        def __init__(self, session: object, *, tenant_id: object | None = None) -> None:
            self._session = session
            self._tenant_id = tenant_id

        @staticmethod
        def _require_month_open(month: str) -> None:
            """Accept only the expected open month for dry-run validation."""
            assert month == MONTH

        @staticmethod
        def upsert_components(*, month: str, components: list[object]) -> None:
            """Reject writes because this dry-run path must not persist components."""
            raise AssertionError("dry-run source filtering must not upsert")

    class _FakePaymentRepository:
        """Payment adapter stub that records selected reads."""

        def __init__(self, session: object, *, tenant_id: object | None = None) -> None:
            self._session = session
            self._tenant_id = tenant_id

        @staticmethod
        def list_month_payments(*, month: str) -> list[object]:
            """Record payment reads for source-filter assertions."""
            reads.append("payments")
            if "payments" not in expected_reads:
                raise AssertionError("payment storage was read for another source")
            return []

    class _FakeBankRepository:
        """Bank adapter stub that records selected reads."""

        def __init__(self, session: object, *, tenant_id: object | None = None) -> None:
            self._session = session
            self._tenant_id = tenant_id

        @staticmethod
        def list_month_entries(*, month: str) -> list[object]:
            """Record bank reconciliation reads for source-filter assertions."""
            reads.append("bank")
            if "bank" not in expected_reads:
                raise AssertionError("bank storage was read for another source")
            return []

    class _FakeSourceRowRepository:
        """Source-row adapter stub that records selected reads."""

        def __init__(self, session: object) -> None:
            self._session = session

        @staticmethod
        def list(tenant_id: UUID, *, report_month: str) -> list[object]:
            """Record source-row reads for source-filter assertions."""
            reads.append("source_rows")
            if "source_rows" not in expected_reads:
                raise AssertionError("source-row storage was read for another source")
            return []

    monkeypatch.setattr(module, "SqlAlchemyDeductionComponentRepository", _FakeRepository)
    monkeypatch.setattr(
        module,
        "get_month_close_status",
        lambda session, month, *, tenant_id=None: None,
        raising=False,
    )
    monkeypatch.setattr(module, "SqlAlchemyAdSensePaymentRepository", _FakePaymentRepository)
    monkeypatch.setattr(module, "SqlAlchemyBankReconciliationRepository", _FakeBankRepository)
    monkeypatch.setattr(
        module,
        "SqlAlchemyGoogleRevenueSourceRowRepository",
        _FakeSourceRowRepository,
    )

    service = module.DeductionIngestionService(object(), audit_sink=InMemoryAuditSink())
    result = service.ingest(
        month=MONTH,
        actor=_actor(),
        reason="r",
        source=selected_source,
        dry_run=True,
    )

    assert result.total_upserted == 0
    assert set(reads) == expected_reads


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


def _add_component(
    session,
    *,
    scope_kind,
    scope_id,
    component_kind="DEDUCTION",
    amount="10.00",
    source_system="adsense_management",
    key=None,
):
    """Insert one DeductionComponentORM row for the shared MONTH."""
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=_ums_tenant(),
            month=MONTH,
            component_kind=component_kind,
            scope_kind=scope_kind,
            scope_id=scope_id,
            amount_usd=Decimal(amount),
            currency_code="USD",
            source_system=source_system,
            source_table="google_revenue_source_rows",
            component_key=key or f"{scope_kind}:{scope_id}:{component_kind}",
            raw_payload={},
        )
    )


def test_list_account_components_returns_only_account_scope(tmp_path):
    """Only scope_kind == ACCOUNT rows are returned; CHANNEL/PAYMENT excluded."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-1")
        _add_component(session, scope_kind="CHANNEL", scope_id="chan-1")
        _add_component(
            session, scope_kind="PAYMENT", scope_id="BANK-1",
            source_system="bank_reconciliation",
        )
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        rows = repo.list_account_components(month=MONTH)
    assert [r.scope_kind for r in rows] == ["ACCOUNT"]
    assert rows[0].scope_id == "pub-1"


def test_list_account_components_filters_by_account(tmp_path):
    """The optional adsense_account_id narrows to one account's scope_id."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-1", key="a")
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-2", key="b")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        rows = repo.list_account_components(month=MONTH, adsense_account_id="pub-2")
    assert [r.scope_id for r in rows] == ["pub-2"]


def test_list_account_components_rejects_malformed_month(tmp_path):
    """A malformed month raises DeductionComponentValidationError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(_mod().DeductionComponentValidationError):
            repo.list_account_components(month="2026-13")
