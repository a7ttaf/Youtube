"""ORM shape tests for source_models.CurrencyORM and
source_models.GoogleRevenueSourceRowORM.

These are SQLite-friendly assertions via metadata.create_all(). The
PostgreSQL-backed migration round-trip lives at
tests/db/test_google_revenue_source_migration.py.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Integer,
    Text,
    UniqueConstraint,
)

from ums_smart_revenue.db.source_models import CurrencyORM


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
    uniques = [
        c for c in CurrencyORM.__table__.constraints if isinstance(c, UniqueConstraint)
    ]
    named = {c.name for c in uniques}
    assert "uq_currencies_numeric_code" in named


def test_currency_orm_checks() -> None:
    checks = [
        c for c in CurrencyORM.__table__.constraints if isinstance(c, CheckConstraint)
    ]
    names = {c.name for c in checks}
    assert "ck_currencies_code_format" in names
    assert "ck_currencies_numeric_code_format" in names
    assert "ck_currencies_minor_unit_range" in names
    assert "ck_currencies_supported_minor" in names
    assert "ck_currencies_supported_activated" in names
