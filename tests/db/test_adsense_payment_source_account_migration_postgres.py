"""PostgreSQL-backed round-trip test for 20260529_0001.

upgrade head (20260527_0001) -> upgrade 20260529_0001 (adds
source_account_id, re-keys uniqueness) and the reverse downgrade. Verifies
the live Postgres schema matches Task 1's AdSensePaymentORM contract.
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
    """Fixture to provide the PostgreSQL database URL for testing."""
    # FIX: resolve UMS_TEST_DATABASE_URL inside a fixture, not at module import.
    # pytest imports every test module during collection, so doing the lookup at
    # import time turned a missing optional Postgres dependency into a *collection*
    # error that aborts the entire `pytest -q` run (0 tests executed) on any
    # machine/CI without Postgres. Resolving here localises the failure to this
    # suite — a hard RuntimeError, never a skip (the no-skip policy gate still
    # holds) — while every unrelated test still collects and runs.
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Fixture to build an Alembic Config pointing to the test database and migrations."""
    # Build Config WITHOUT passing the alembic.ini path. env.py only calls
    # `logging.config.fileConfig()` when `config.config_file_name is not None`.
    # `fileConfig` defaults to `disable_existing_loggers=True`, which silences
    # pytest's `caplog` fixture for every later test in the session and breaks
    # unrelated tenancy log-assertion tests (`tests/tenancy/test_resolver.py`).
    # Setting `script_location` and `sqlalchemy.url` directly gives alembic
    # everything it needs without touching the logging tree.
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str) -> object:
    """Fixture to create a fresh database engine by resetting the public schema."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_source_account_migration_rekeys_uniqueness(
    alembic_config, fresh_engine
) -> None:
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    cols = {c["name"]: c for c in inspector.get_columns("adsense_payments")}
    assert cols["source_account_id"]["nullable"] is False
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("adsense_payments")
    }
    assert uniques["uq_adsense_payments_account_month_name"] == (
        "tenant_id", "source_account_id", "month", "payment_name",
    )
    assert "uq_adsense_payments_month_name" not in uniques
    checks = {c["name"] for c in inspector.get_check_constraints("adsense_payments")}
    assert "ck_adsense_payments_source_account_id_nonempty" in checks


def test_source_account_migration_downgrade_reverses(
    alembic_config, fresh_engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "-1")
    inspector = inspect(fresh_engine)
    cols = {c["name"] for c in inspector.get_columns("adsense_payments")}
    assert "source_account_id" not in cols
    uniques = {c["name"] for c in inspector.get_unique_constraints("adsense_payments")}
    assert "uq_adsense_payments_month_name" in uniques
    # The re-keyed account uniqueness and the non-empty CHECK must be fully
    # reversed too, not merely the column drop — downgrade is a clean inverse.
    assert "uq_adsense_payments_account_month_name" not in uniques
    checks = {c["name"] for c in inspector.get_check_constraints("adsense_payments")}
    assert "ck_adsense_payments_source_account_id_nonempty" not in checks
