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

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_upgrade_creates_table_constraints_and_indexes(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    cols = {c["name"]: c for c in inspector.get_columns("deduction_components")}
    assert cols["amount_usd"]["nullable"] is False
    assert cols["amount_native"]["nullable"] is True
    assert cols["raw_payload"]["nullable"] is False
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("deduction_components")
    }
    assert uniques["uq_deduction_components_key"] == ("tenant_id", "component_key")
    checks = {c["name"] for c in inspector.get_check_constraints("deduction_components")}
    assert "ck_deduction_components_kind" in checks
    assert "ck_deduction_components_scope_kind" in checks
    assert "ck_deduction_components_amount_usd_finite" in checks
    assert "ck_deduction_components_raw_payload_object" in checks
    indexes = {c["name"] for c in inspector.get_indexes("deduction_components")}
    assert "ix_deduction_components_tenant_month" in indexes
    assert "ix_deduction_components_tenant_scope" in indexes


def test_downgrade_drops_table(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    inspector = inspect(fresh_engine)
    assert "deduction_components" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "deduction_components" in inspector.get_table_names()


def test_duplicate_component_key_rejected_by_unique(alembic_config, fresh_engine):
    """Behavioral: the (tenant_id, component_key) idempotency contract is enforced."""
    from sqlalchemy.exc import IntegrityError

    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO deduction_components "
        "(tenant_id, month, component_kind, scope_kind, scope_id, amount_usd, "
        "currency_code, source_system, source_table, component_key) VALUES "
        "(:tenant, '2026-04', 'TRANSFER_FEE', 'PAYMENT', 'BANK-1', 3.50, 'USD', "
        "'bank_reconciliation', 'bank_reconciliation_entries', "
        "'bank:2026-04:BANK-1:transfer_fee')"
    )
    tenant = "00000000-0000-0000-0000-0000000000aa"
    with fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": tenant})
    with pytest.raises(IntegrityError):
        with fresh_engine.begin() as conn:
            conn.execute(insert_sql, {"tenant": tenant})
