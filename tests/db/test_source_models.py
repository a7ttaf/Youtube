"""ORM shape tests for source_models.CurrencyORM and
source_models.GoogleRevenueSourceRowORM.

These are SQLite-friendly assertions via metadata.create_all(). The
PostgreSQL-backed migration round-trip lives at
tests/db/test_google_revenue_source_migration_postgres.py.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)


def test_currency_orm_table_name() -> None:
    assert CurrencyORM.__tablename__ == "currencies"


def test_currency_orm_columns() -> None:
    columns = {column.name: column for column in CurrencyORM.__table__.columns}
    assert set(columns) == {
        "code",
        "numeric_code",
        "name",
        "minor_unit",
        "is_supported",
        "activated_at",
    }
    assert columns["code"].primary_key is True
    assert isinstance(columns["code"].type, Text)
    assert isinstance(columns["numeric_code"].type, Text)
    assert isinstance(columns["name"].type, Text)
    assert isinstance(columns["minor_unit"].type, Integer)
    assert columns["minor_unit"].nullable is True
    assert isinstance(columns["is_supported"].type, Boolean)
    assert columns["is_supported"].nullable is False
    assert columns["activated_at"].nullable is True


def test_currency_orm_unique_numeric_code() -> None:
    uniques = [c for c in CurrencyORM.__table__.constraints if isinstance(c, UniqueConstraint)]
    named = {c.name for c in uniques}
    assert "uq_currencies_numeric_code" in named


def test_currency_orm_checks() -> None:
    checks = [c for c in CurrencyORM.__table__.constraints if isinstance(c, CheckConstraint)]
    names = {c.name for c in checks}
    assert "ck_currencies_code_format" in names
    assert "ck_currencies_numeric_code_format" in names
    assert "ck_currencies_minor_unit_range" in names
    assert "ck_currencies_supported_minor" in names
    assert "ck_currencies_supported_activated" in names


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()


def test_insert_and_select_currency_row(session: Session) -> None:
    row = CurrencyORM(
        code="USD",
        numeric_code="840",
        name="US Dollar",
        minor_unit=2,
        is_supported=False,
    )
    session.add(row)
    session.flush()
    reloaded = session.scalar(select(CurrencyORM).where(CurrencyORM.code == "USD"))
    assert reloaded is not None
    assert reloaded.numeric_code == "840"
    assert reloaded.minor_unit == 2
    assert reloaded.is_supported is False
    assert reloaded.activated_at is None


def test_google_revenue_source_row_table_name() -> None:
    assert GoogleRevenueSourceRowORM.__tablename__ == "google_revenue_source_rows"


def test_google_revenue_source_row_columns() -> None:
    columns = {c.name: c for c in GoogleRevenueSourceRowORM.__table__.columns}
    expected = {
        "id",
        "tenant_id",
        "source_system",
        "source_row_key",
        "source_account_id",
        "content_owner_id",
        "youtube_channel_id",
        "report_type",
        "report_month",
        "period_start",
        "period_end",
        "metric_key",
        "value_kind",
        "amount_native",
        "currency_code",
        "source_report_id",
        "raw_file_id",
        "raw_payload",
        "imported_by",
        "ingested_at",
    }
    assert set(columns) == expected
    assert columns["id"].primary_key is True
    assert columns["tenant_id"].nullable is False
    assert columns["source_system"].nullable is False
    assert columns["source_row_key"].nullable is False
    assert columns["source_account_id"].nullable is False
    assert columns["content_owner_id"].nullable is True
    assert columns["youtube_channel_id"].nullable is True
    assert columns["report_type"].nullable is False
    assert columns["report_month"].nullable is False
    assert columns["period_start"].nullable is False
    assert columns["period_end"].nullable is False
    assert columns["metric_key"].nullable is False
    assert columns["value_kind"].nullable is False
    assert columns["amount_native"].nullable is False
    assert columns["currency_code"].nullable is False
    assert columns["source_report_id"].nullable is True
    assert columns["raw_file_id"].nullable is True
    assert columns["raw_payload"].nullable is False
    assert columns["imported_by"].nullable is True
    assert columns["ingested_at"].nullable is False


def test_google_revenue_source_row_unique_source_row_key() -> None:
    uniques = [
        c
        for c in GoogleRevenueSourceRowORM.__table__.constraints
        if isinstance(c, UniqueConstraint)
    ]
    named = {c.name for c in uniques}
    assert "uq_google_revenue_source_rows_source_key" in named


def test_google_revenue_source_row_tenant_fk_present() -> None:
    fks = [
        c
        for c in GoogleRevenueSourceRowORM.__table__.constraints
        if isinstance(c, ForeignKeyConstraint)
    ]
    target_tables = {fk.referred_table.name for fk in fks}
    assert "tenants" in target_tables
    assert "currencies" in target_tables
    assert "raw_report_files" in target_tables


def test_google_revenue_source_row_indexes() -> None:
    index_names = {ix.name for ix in GoogleRevenueSourceRowORM.__table__.indexes}
    assert "ix_google_revenue_source_rows_tenant_month_source" in index_names
    assert "ix_google_revenue_source_rows_tenant_channel_month" in index_names


def test_report_month_check_rejects_non_digit_month(session: Session) -> None:
    """The report_month CHECK must reject non-digit month characters (e.g.
    '2026-0A'): the prior lexical `substr(...,6,2) BETWEEN '01' AND '12'` alone
    accepted '0A'. SQLite enforces the CHECK on flush.
    """
    row = GoogleRevenueSourceRowORM(
        id=uuid4(),
        tenant_id=uuid4(),
        source_system="youtube_reporting",
        source_row_key="z" * 64,
        source_account_id="acct",
        report_type="channel_basic_a2",
        report_month="2026-0A",  # non-digit month character
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key="estimatedRevenue",
        value_kind="estimated",
        amount_native=Decimal("1.000000"),
        currency_code="USD",
        raw_payload={},
    )
    session.add(row)
    # Narrow the assertion to the report_month CHECK so an unrelated
    # IntegrityError (e.g. a future FK/uniqueness change) cannot silently mask
    # a regression in this constraint. SQLite reports the constraint name on
    # newer builds and the generic "CHECK constraint failed" on older ones.
    with pytest.raises(
        IntegrityError,
        match=r"ck_google_revenue_source_rows_report_month_format|CHECK constraint failed",
    ):
        session.flush()
