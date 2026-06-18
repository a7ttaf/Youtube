from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM


def _engine(tmp_path):
    """Fresh SQLite engine with only finance_month_close for constraint tests."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceMonthCloseORM.__table__.create(engine)
    return engine


def test_finance_metadata_contains_month_close_table():
    assert set(FinanceBase.metadata.tables) >= {"finance_month_close"}


def test_finance_month_close_has_control_columns():
    table = FinanceBase.metadata.tables["finance_month_close"]

    assert {
        "month",
        "status",
        "allocation_method",
        "allocation_rule_payload",
        "locked_by",
        "locked_at",
        "unlocked_by",
        "unlocked_at",
    } <= set(table.columns.keys())
    assert any(
        constraint.name == "ck_finance_month_close_month_format" for constraint in table.constraints
    )
    assert any(
        constraint.name == "ck_finance_month_close_allocation_method"
        for constraint in table.constraints
    )


def test_finance_month_close_allocation_method_check_rejects_unknown(tmp_path):
    """allocation_method CHECK rejects methods outside the shared allowlist."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            FinanceMonthCloseORM(
                month="2026-04",
                status="OPEN",
                allocation_method="definitely_not_a_method",
                allocation_rule_payload={},
            )
        )
        session.commit()


def test_finance_month_close_allocation_method_check_accepts_allowlist_and_null(tmp_path):
    """allocation_method CHECK accepts the five methods and unconfigured NULL rows."""
    engine = _engine(tmp_path)
    methods = (
        None,
        "gross_revenue_proportional",
        "post_tax_revenue_proportional",
        "company_level",
        "manual",
        "no_allocation",
    )
    with Session(engine) as session:
        for index, method in enumerate(methods, start=1):
            session.add(
                FinanceMonthCloseORM(
                    month=f"2026-{index:02d}",
                    status="OPEN",
                    allocation_method=method,
                    allocation_rule_payload={},
                )
            )
        session.commit()
        assert session.query(FinanceMonthCloseORM).count() == len(methods)
