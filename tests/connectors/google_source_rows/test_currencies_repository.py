"""Read-only contract tests for SqlAlchemyCurrenciesRepository.

The repository exposes list_all / list_supported / get only. Any future
admin-side mutation (flipping is_supported, adding a new ISO snapshot)
belongs to a separate admin API with its own audit story (spec section 6).
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows import (
    SqlAlchemyCurrenciesRepository,
)
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import CurrencyORM


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            s.add_all(
                [
                    CurrencyORM(
                        code="USD",
                        numeric_code="840",
                        name="US Dollar",
                        minor_unit=2,
                        is_supported=True,
                        activated_at=datetime.now(UTC),
                    ),
                    CurrencyORM(
                        code="EUR",
                        numeric_code="978",
                        name="Euro",
                        minor_unit=2,
                        is_supported=True,
                        activated_at=datetime.now(UTC),
                    ),
                    CurrencyORM(
                        code="XTS",
                        numeric_code="963",
                        name="Test",
                        minor_unit=None,
                        is_supported=False,
                        activated_at=None,
                    ),
                ]
            )
            s.flush()
            yield s
    finally:
        engine.dispose()


def test_list_all_returns_every_row(session: Session) -> None:
    repo = SqlAlchemyCurrenciesRepository(session)
    rows = repo.list_all()
    assert {r.code for r in rows} == {"USD", "EUR", "XTS"}


def test_list_supported_filters_to_supported_rows(session: Session) -> None:
    repo = SqlAlchemyCurrenciesRepository(session)
    rows = repo.list_supported()
    assert {r.code for r in rows} == {"USD", "EUR"}
    for row in rows:
        assert row.is_supported is True
        assert row.activated_at is not None


def test_get_returns_entry_for_known_code(session: Session) -> None:
    repo = SqlAlchemyCurrenciesRepository(session)
    entry = repo.get("USD")
    assert entry is not None
    assert entry.numeric_code == "840"


def test_get_returns_none_for_unknown_code(session: Session) -> None:
    repo = SqlAlchemyCurrenciesRepository(session)
    assert repo.get("ZZZ") is None


def test_repository_has_no_write_method(session: Session) -> None:
    repo = SqlAlchemyCurrenciesRepository(session)
    assert not hasattr(repo, "set_supported")
    assert not hasattr(repo, "create")
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
