"""PostgreSQL round-trip for 20260529_0002 (deduction_components).

upgrade 20260529_0001 -> 20260529_0002 (create deduction_components) and the
reverse downgrade. Verifies the live Postgres schema matches DeductionComponentORM.
"""
from pathlib import Path

import pytest
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_url() -> str:
    """Return the PostgreSQL database URL for testing."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Create an Alembic Config object configured with the test Postgres URL and script location."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    """Provide a fresh SQLAlchemy engine with a clean public schema for each test."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_upgrade_creates_table_constraints_and_indexes(alembic_config, fresh_engine):
    """Verify upgrade to head creates the deduction_components table, constraints, and indexes."""
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    cols = {c["name"]: c for c in inspector.get_columns("deduction_components")}
    assert UMS_TENANT_ID in str(cols["tenant_id"]["default"])
    assert cols["amount_usd"]["nullable"] is False
    assert cols["amount_usd"]["type"].precision == 20
    assert cols["amount_usd"]["type"].scale == 6
    assert cols["amount_native"]["nullable"] is True
    assert cols["raw_payload"]["nullable"] is False
    foreign_keys = {
        c["name"]: c for c in inspector.get_foreign_keys("deduction_components")
    }
    assert foreign_keys["fk_deduction_components_tenant"]["referred_table"] == "tenants"
    assert foreign_keys["fk_deduction_components_tenant"]["referred_columns"] == ["id"]
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("deduction_components")
    }
    assert uniques["uq_deduction_components_key"] == ("tenant_id", "component_key")
    checks = {
        c["name"]: c["sqltext"]
        for c in inspector.get_check_constraints("deduction_components")
    }
    assert "ck_deduction_components_kind" in checks
    assert "ck_deduction_components_scope_kind" in checks
    assert "ck_deduction_components_amount_usd_finite" in checks
    assert "ck_deduction_components_amount_native_finite" in checks
    assert "ck_deduction_components_raw_payload_object" in checks
    assert "substr(month, 6, 1)" in checks["ck_deduction_components_month_format"]
    assert "substr(month, 7, 1)" in checks["ck_deduction_components_month_format"]
    assert "currency_code = 'USD'" in checks["ck_deduction_components_currency_code"]
    indexes = {c["name"] for c in inspector.get_indexes("deduction_components")}
    assert "ix_deduction_components_tenant_month" in indexes
    assert "ix_deduction_components_tenant_scope" in indexes
    assert "ix_deduction_components_tenant_month_kind" in indexes


def test_downgrade_drops_table(alembic_config, fresh_engine):
    """Verify downgrade removes the deduction_components table from the database."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    inspector = inspect(fresh_engine)
    assert "deduction_components" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    """Verify upgrade, downgrade, then upgrade again maintains consistency of the schema."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "deduction_components" in inspector.get_table_names()


def test_duplicate_component_key_rejected_by_unique(alembic_config, fresh_engine):
    """Behavioral: the (tenant_id, component_key) idempotency contract is enforced."""
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO deduction_components "
        "(tenant_id, month, component_kind, scope_kind, scope_id, amount_usd, "
        "currency_code, source_system, source_table, component_key) VALUES "
        "(:tenant, '2026-04', 'TRANSFER_FEE', 'PAYMENT', 'BANK-1', 3.50, 'USD', "
        "'bank_reconciliation', 'bank_reconciliation_entries', "
        "'bank:2026-04:BANK-1:transfer_fee')"
    )
    with fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})


@pytest.mark.parametrize(
    ("month", "currency", "component_key"),
    [
        ("2026-0A", "USD", "bad-month-suffix"),
        ("2026-04", "EUR", "bad-currency"),
    ],
)
def test_month_and_currency_constraints_reject_invalid_values(
    alembic_config,
    fresh_engine,
    month,
    currency,
    component_key,
):
    """Behavioral: malformed months and non-USD currencies are rejected."""
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO deduction_components "
        "(tenant_id, month, component_kind, scope_kind, scope_id, amount_usd, "
        "currency_code, source_system, source_table, component_key) VALUES "
        "(:tenant, :month, 'TRANSFER_FEE', 'PAYMENT', 'BANK-1', 3.50, "
        ":currency, 'bank_reconciliation', 'bank_reconciliation_entries', "
        ":component_key)"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(
            insert_sql,
            {
                "tenant": UMS_TENANT_ID,
                "month": month,
                "currency": currency,
                "component_key": component_key,
            },
        )


def test_orphan_tenant_rejected_by_foreign_key(alembic_config, fresh_engine):
    """Behavioral: deduction rows must reference a real tenant."""
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO deduction_components "
        "(tenant_id, month, component_kind, scope_kind, scope_id, amount_usd, "
        "currency_code, source_system, source_table, component_key) VALUES "
        "('00000000-0000-0000-0000-0000000000aa', '2026-04', "
        "'TRANSFER_FEE', 'PAYMENT', 'BANK-1', 3.50, 'USD', "
        "'bank_reconciliation', 'bank_reconciliation_entries', 'orphan-tenant')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql)
