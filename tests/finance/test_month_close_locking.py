from types import SimpleNamespace

import pytest

from ums_smart_revenue.finance import manual_overrides as manual_overrides_module
from ums_smart_revenue.finance import month_close as month_close_module
from ums_smart_revenue.finance import month_close_readiness as readiness_module
from ums_smart_revenue.finance import revenue_facts as revenue_facts_module
from ums_smart_revenue.finance.month_close import (
    acquire_finance_month_advisory_lock,
    get_or_create_month_close_row,
)
from ums_smart_revenue.finance.month_close_readiness import (
    SqlAlchemyFinanceCloseReadinessService,
)


class _DialectSession:
    def __init__(self, dialect_name: str) -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.executed: list[tuple[object, dict[str, object]]] = []

    def get_bind(self) -> SimpleNamespace:
        return self._bind

    def execute(self, statement: object, parameters: dict[str, object]) -> None:
        self.executed.append((statement, parameters))


def test_finance_month_advisory_lock_uses_postgres_transaction_lock() -> None:
    """Postgres close attempts acquire a stable transaction-scoped month lock."""
    session = _DialectSession("postgresql")

    acquire_finance_month_advisory_lock(session, "2026-03")

    statement, parameters = session.executed[0]
    second_session = _DialectSession("postgresql")
    acquire_finance_month_advisory_lock(second_session, "2026-03")

    assert "pg_advisory_xact_lock" in str(statement)
    assert isinstance(parameters["lock_key"], int)
    assert parameters["lock_key"] == second_session.executed[0][1]["lock_key"]


def test_finance_month_advisory_lock_is_noop_for_sqlite_tests() -> None:
    """SQLite-backed tests keep the close-row lock path without PG SQL."""
    session = _DialectSession("sqlite")

    acquire_finance_month_advisory_lock(session, "2026-03")

    assert session.executed == []


def test_get_or_create_month_close_row_acquires_guard_before_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Month-scoped writers serialize before taking the close-row lock."""
    calls: list[tuple[str, object]] = []

    class ScalarRows:
        def one_or_none(self) -> SimpleNamespace:
            calls.append(("fetch", None))
            return SimpleNamespace(month="2026-03", status="OPEN")

    class Session:
        def scalars(self, statement: object) -> ScalarRows:
            calls.append(("select", statement))
            return ScalarRows()

    def record_guard(session: object, month: str) -> None:
        del session
        calls.append(("guard", month))

    monkeypatch.setattr(
        month_close_module,
        "acquire_finance_month_advisory_lock",
        record_guard,
    )

    row = get_or_create_month_close_row(Session(), "2026-03", for_update=True)

    assert row.month == "2026-03"
    assert calls[0] == ("guard", "2026-03")
    assert calls[1][0] == "select"


def test_for_update_readiness_acquires_guard_before_blocker_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-time readiness holds the month guard before blocker reads."""
    calls: list[tuple[str, object]] = []

    def record_guard(session: object, month: str) -> None:
        del session
        calls.append(("guard", month))

    def record_pending(self: object, month: str, *, for_update: bool) -> int:
        del self, month
        calls.append(("pending", for_update))
        return 0

    def record_missing(self: object, month: str, *, for_update: bool) -> int:
        del self, month
        calls.append(("missing", for_update))
        return 0

    def record_facts(self: object, month: str, *, for_update: bool) -> list[object]:
        del self, month
        calls.append(("facts", for_update))
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
        ("guard", "2026-03"),
        ("pending", True),
        ("missing", True),
        ("facts", True),
    ]


def test_revenue_fact_writes_use_guarded_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revenue fact imports funnel through the guarded month-open check."""
    calls: list[tuple[str, bool]] = []

    def record_get_or_create(
        session: object, month: str, *, for_update: bool
    ) -> SimpleNamespace:
        del session
        calls.append((month, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        revenue_facts_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )

    revenue_facts_module.SqlAlchemyRevenueFactRepository(object())._require_month_open(
        "2026-03"
    )

    assert calls == [("2026-03", True)]


def test_manual_override_writes_use_guarded_month_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual override mutations funnel through the guarded month-open check."""
    calls: list[tuple[str, bool]] = []

    def record_get_or_create(
        session: object, month: str, *, for_update: bool
    ) -> SimpleNamespace:
        del session
        calls.append((month, for_update))
        return SimpleNamespace(status="OPEN")

    monkeypatch.setattr(
        manual_overrides_module,
        "get_or_create_month_close_row",
        record_get_or_create,
    )

    manual_overrides_module.SqlAlchemyManualOverrideRepository(
        object()
    )._require_month_open("2026-03")

    assert calls == [("2026-03", True)]
