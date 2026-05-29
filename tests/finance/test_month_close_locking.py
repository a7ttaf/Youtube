from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.finance import adsense_payments as adsense_payments_module
from ums_smart_revenue.finance import bank_reconciliation as bank_reconciliation_module
from ums_smart_revenue.finance import manual_overrides as manual_overrides_module
from ums_smart_revenue.finance import month_close as month_close_module
from ums_smart_revenue.finance import month_close_readiness as readiness_module
from ums_smart_revenue.finance import revenue_facts as revenue_facts_module
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationValidationError,
"""Unit tests for finance month advisory lock using a dialect session stub and tenant fixtures."""

    SqlAlchemyBankReconciliationRepository,
)
from ums_smart_revenue.finance.manual_overrides import ManualOverrideValidationError
from ums_smart_revenue.finance.month_close import (
    SqlAlchemyFinanceMonthCloseRepository,
    acquire_finance_month_advisory_lock,
    get_or_create_month_close_row,
)
from ums_smart_revenue.finance.month_close_readiness import (
    SqlAlchemyFinanceCloseReadinessService,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-00000000f301")


class _DialectSession:
    """A session stub that simulates SQLAlchemy session dialect binding for tests by capturing executed statements."""
    def __init__(self, dialect_name: str) -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.executed: list[tuple[object, dict[str, object]]] = []

    def get_bind(self) -> SimpleNamespace:
        """Return the session's SQLAlchemy dialect bind stub."""
        return self._bind

    def execute(self, statement: object, parameters: dict[str, object]) -> None:
        """Record an executed statement and its parameters in the executed list."""
        self.executed.append((statement, parameters))


def _tenant(tenant_id: UUID = OTHER_TENANT_ID) -> Tenant:
    """Create and return a Tenant object with default active status and timestamps for testing."""
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug=f"tenant-{tenant_id.hex[-4:]}",
        display_name="Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )

def test_finance_month_advisory_lock_uses_postgres_transaction_lock() -> None:
    """Postgres close attempts acquire a stable transaction-scoped month lock."""
    session = _DialectSession("postgresql")

    acquire_finance_month_advisory_lock(session, "2026-03")
    assert len(session.executed) == 1

    statement, parameters = session.executed[0]
    second_session = _DialectSession("postgresql")
    acquire_finance_month_advisory_lock(second_session, "2026-03")
    assert len(second_session.executed) == 1
    other_month_session = _DialectSession("postgresql")
    acquire_finance_month_advisory_lock(other_month_session, "2026-04")
    assert len(other_month_session.executed) == 1
    other_tenant_session = _DialectSession("postgresql")
    acquire_finance_month_advisory_lock(
        other_tenant_session,
        "2026-03",
        tenant_id=UUID("00000000-0000-0000-0000-00000000f102"),
    )
    assert len(other_tenant_session.executed) == 1

    assert "pg_advisory_xact_lock" in str(statement)
    assert isinstance(parameters["lock_key"], int)
    assert parameters["lock_key"] == second_session.executed[0][1]["lock_key"]
    assert isinstance(other_month_session.executed[0][1]["lock_key"], int)
    assert parameters["lock_key"] != other_month_session.executed[0][1]["lock_key"]
    assert isinstance(other_tenant_session.executed[0][1]["lock_key"], int)
    assert parameters["lock_key"] != other_tenant_session.executed[0][1]["lock_key"]


def test_finance_month_advisory_lock_is_noop_for_sqlite_tests() -> None:
    """SQLite-backed tests keep the close-row lock path without PG SQL."""
    session = _DialectSession("sqlite")

    acquire_finance_month_advisory_lock(session, "2026-03")

    assert session.executed == []


"""
Tests for month close locking logic in the finance module, ensuring correct lock acquisition and row creation behavior.
"""

def test_get_or_create_month_close_row_acquires_guard_before_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Month-scoped writers serialize before taking the close-row lock."""
    calls: list[tuple[str, object]] = []

    class ScalarRows:
        """
        Represents a collection of rows returned by a scalar database query,
        allowing retrieval of zero or one result.
        """
        @staticmethod
        def one_or_none() -> SimpleNamespace:
            """Return a single result or None for the scalar query; simulates fetching month close status."""
            calls.append(("fetch", None))
            return SimpleNamespace(month="2026-03", status="OPEN")

    class Session:
        """
        Mock session class to simulate database session interactions for testing,
        providing scalar query execution returning ScalarRows.
        """
        @staticmethod
        def scalars(statement: object) -> ScalarRows:
            """Execute the given statement and return a ScalarRows object representing query results."""
            calls.append(("select", statement))
            return ScalarRows()

    def record_guard(
        session: object, month: str, *, tenant_id: UUID | str | None = None
    ) -> None:
        """Acquire a record-level guard for the specified month and tenant to prevent concurrent access."""
        calls.append(("guard", (month, tenant_id)))

    monkeypatch.setattr(
        month_close_module,
        "acquire_finance_month_advisory_lock",
        record_guard,
    )

    row = get_or_create_month_close_row(Session(), "2026-03", for_update=True)

    assert row.month == "2026-03"
    assert calls[0] == ("guard", ("2026-03", DEFAULT_TENANT_ID))
    assert calls[1][0] == "select"


def test_get_or_create_month_close_row_uses_explicit_tenant() -> None:
    """Month close rows are keyed by the tenant supplied by tenant-aware callers."""
    tenant_id = UUID("00000000-0000-0000-0000-00000000f201")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        row = get_or_create_month_close_row(session, "2026-03", tenant_id=tenant_id)

        assert row.tenant_id == tenant_id
        assert session.get(FinanceMonthCloseORM, (tenant_id, "2026-03")) is row


def test_get_or_create_month_close_row_uses_request_tenant_context() -> None:
    """Public close-row helpers honor request context when tenant_id is omitted."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    token = TENANT_CTX.set(_tenant())
    try:
        with Session(engine) as session:
            row = get_or_create_month_close_row(session, "2026-03")

            assert row.tenant_id == OTHER_TENANT_ID
            assert (
                session.get(
                    FinanceMonthCloseORM,
                    (OTHER_TENANT_ID, "2026-03"),
                )
                is row
            )
            assert (
                session.get(
                    FinanceMonthCloseORM,
                    (DEFAULT_TENANT_ID, "2026-03"),
                )
                is None
            )
    finally:
        TENANT_CTX.reset(token)


def test_finance_month_advisory_lock_uses_request_tenant_context() -> None:
    """Omitted advisory-lock tenant ids resolve to the active request tenant."""
    default_session = _DialectSession("postgresql")
    acquire_finance_month_advisory_lock(default_session, "2026-03")
    request_session = _DialectSession("postgresql")
    token = TENANT_CTX.set(_tenant())
    try:
        acquire_finance_month_advisory_lock(request_session, "2026-03")
    finally:
        TENANT_CTX.reset(token)

    assert (
        default_session.executed[0][1]["lock_key"]
        != (request_session.executed[0][1]["lock_key"])
    )


def test_finance_month_close_repository_uses_explicit_tenant() -> None:
    """The close repository reads and writes the tenant it is bound to."""
    tenant_id = UUID("00000000-0000-0000-0000-00000000f202")
    actor_user_id = "00000000-0000-0000-0000-00000000f203"
"""Test module for finance month close locking behavior.
Contains tests for verifying advisory lock acquisition and readiness checks."""

engine = create_engine("sqlite+pysqlite:///:memory:")
FinanceBase.metadata.create_all(engine)

with Session(engine) as session:
    repository = SqlAlchemyFinanceMonthCloseRepository(session, tenant_id=tenant_id)
    entry = repository.lock_month(month="2026-03", actor_user_id=actor_user_id)

    tenant_row = session.get(FinanceMonthCloseORM, (tenant_id, "2026-03"))
    default_row = session.get(
        FinanceMonthCloseORM,
        (DEFAULT_TENANT_ID, "2026-03"),
    )

assert entry.status == "LOCKED"
assert tenant_row is not None
assert tenant_row.status == "LOCKED"
assert default_row is None

def test_for_update_readiness_acquires_guard_before_blocker_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-time readiness holds the month guard before blocker reads."""
    calls: list[tuple[str, object]] = []

    def record_guard(
        session: object, month: str, *, tenant_id: UUID | str | None = None
    ) -> None:
        """Record guard invocation for finance month lock, appending month and tenant_id to calls."""
        del session
        calls.append(("guard", (month, tenant_id)))

    def record_pending(self: object, month: str, *, for_update: bool) -> int:
        """Record pending manual override check invocation, appending for_update flag to calls and returning mock count."""
        calls.append(("pending", for_update))
        return 0

    def record_missing(self: object, month: str, *, for_update: bool) -> int:
        """Record missing required revenue fact count invocation, appending for_update flag to calls and returning mock count."""
        calls.append(("missing", for_update))
        return 0

    def record_facts(self: object, month: str, *, for_update: bool) -> list[object]:
        """Record month facts retrieval invocation, appending for_update flag to calls and returning empty list."""
        calls.append(("facts", for_update))
        return []
        return []

    monkeypatch.setattr(
        readiness_module,
        "acquire_finance_month_advisory_lock",
        record_guard,
    )
    monkeypatch.setattr(
        SqlAlchemyFinanceCloseReadinessService,
        "_pending_manual_override_count",
        record_pending,
    )
    monkeypatch.setattr(
        SqlAlchemyFinanceCloseReadinessService,
        "_missing_required_revenue_fact_count",
        record_missing,
    )
    monkeypatch.setattr(
        SqlAlchemyFinanceCloseReadinessService,
        "_month_facts",
        record_facts,
    )

    readiness = SqlAlchemyFinanceCloseReadinessService(object()).check_month(
        "2026-03", for_update=True
    )

    assert readiness.ready is True
    assert calls == [
        ("guard", ("2026-03", DEFAULT_TENANT_ID)),
        ("pending", True),
        ("missing", True),
        ("facts", True),
    ]


def test_revenue_fact_writes_use_guarded_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revenue fact imports funnel through the guarded month-open check."""
    calls: list[tuple[str, UUID | str | None, bool]] = []

    def record_get_or_create(
        session: object,
        month: str,
        *,
        tenant_id: UUID | str | None = None,
        for_update: bool,
    ) -> SimpleNamespace:
        """Simulate retrieving or creating a month-close record for testing purposes.
        Records the month, tenant_id, and for_update flag, and returns an OPEN status."""
        calls.append((month, tenant_id, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        revenue_facts_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )

    revenue_facts_module.SqlAlchemyRevenueFactRepository(object())._require_month_open(
        "2026-03"
    )

    assert calls == [("2026-03", DEFAULT_TENANT_ID, True)]


"""Module containing tests for month-close locking behavior in SqlAlchemyManualOverrideRepository."""

def test_manual_override_writes_use_guarded_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual override mutations funnel through the guarded month-open check."""
    calls: list[tuple[str, UUID | str | None, bool]] = []

    def record_get_or_create(
        session: object,
        month: str,
        *,
        tenant_id: UUID | str | None = None,
        for_update: bool,
    ) -> SimpleNamespace:
        """Fake implementation capturing calls and returning an OPEN status row."""
        calls.append((month, tenant_id, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        manual_overrides_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )

    manual_overrides_module.SqlAlchemyManualOverrideRepository(
        object()
    )._require_month_open("2026-03")

    assert calls == [("2026-03", DEFAULT_TENANT_ID, True)]


"""
Finance tests for month close locking logic in AdSense payments.
"""

def test_adsense_payment_writes_use_context_tenant_in_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AdSense payment sync checks the close row for its resolved tenant."""
    calls: list[tuple[str, UUID | str | None, bool]] = []

    def record_get_or_create(
        session: object,
        month: str,
        *,
        tenant_id: UUID | str | None = None,
        for_update: bool,
    ) -> SimpleNamespace:
        """Mock function to get or create a month close row and record invocation."""
        calls.append((month, tenant_id, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        adsense_payments_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )
    token = TENANT_CTX.set(_tenant())
    try:
        SqlAlchemyAdSensePaymentRepository(object())._require_month_open("2026-03")
    finally:
        TENANT_CTX.reset(token)

    assert calls == [("2026-03", OTHER_TENANT_ID, True)]


def test_bank_reconciliation_writes_use_context_tenant_in_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bank reconciliation writes check the close row for their resolved tenant."""
    calls: list[tuple[str, UUID | str | None, bool]] = []

    def record_get_or_create(
        session: object,
        month: str,
        *,
        tenant_id: UUID | str | None = None,
        for_update: bool,
    ) -> SimpleNamespace:
        """Simulated get_or_create_month_close_row that records its arguments and returns an OPEN status."""
        calls.append((month, tenant_id, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        bank_reconciliation_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )
    token = TENANT_CTX.set(_tenant())
    try:
        SqlAlchemyBankReconciliationRepository(object())._require_month_open("2026-03")
    finally:
        TENANT_CTX.reset(token)

    assert calls == [("2026-03", OTHER_TENANT_ID, True)]


def test_tenant_id_constructor_inputs_are_validated() -> None:
    """Tenant-aware finance repositories reject malformed tenant_id strings."""
    with pytest.raises(ValueError, match="tenant_id must be a valid UUID"):
        SqlAlchemyFinanceMonthCloseRepository(object(), tenant_id=" ")
    with pytest.raises(ValueError, match="tenant_id must be a valid UUID"):
        SqlAlchemyFinanceCloseReadinessService(object(), tenant_id=" ")
    with pytest.raises(ManualOverrideValidationError, match="valid UUID"):
        manual_overrides_module.SqlAlchemyManualOverrideRepository(
            object(),
            tenant_id="not-a-uuid",
        )
    with pytest.raises(RevenueFactValidationError, match="valid UUID"):
        revenue_facts_module.SqlAlchemyRevenueFactRepository(
            object(),
            tenant_id="not-a-uuid",
        )
    with pytest.raises(AdSensePaymentValidationError, match="valid UUID"):
        SqlAlchemyAdSensePaymentRepository(object(), tenant_id="not-a-uuid")
    with pytest.raises(BankReconciliationValidationError, match="valid UUID"):
        SqlAlchemyBankReconciliationRepository(object(), tenant_id="not-a-uuid")
