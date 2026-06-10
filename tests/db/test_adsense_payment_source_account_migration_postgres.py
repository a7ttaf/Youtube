"""PostgreSQL-backed round-trip test for 20260529_0001.

upgrade head (20260527_0001) -> upgrade 20260529_0001 (adds
source_account_id, re-keys uniqueness) and the reverse downgrade. Verifies
the live Postgres schema matches Task 1's AdSensePaymentORM contract.
"""

from pathlib import Path

import pytest
from _pg_schema_helpers import reset_public_schema
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
    # ============================================================================
    # Purpose: Provide a fresh SQLAlchemy engine with a clean `public` schema
    # for one migration round-trip test. The schema-reset body is delegated
    # to the shared helper so a contended reset (e.g. an orphan
    # `idle in transaction` connection from a prior killed/hung run on a
    # shared/reused cluster) fails fast with a diagnosable `LockNotAvailable`
    # error instead of hanging indefinitely.
    # Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`,
    # executed inside the shared `reset_public_schema` helper.
    # Standards: `SET LOCAL lock_timeout = '30s'` is transaction-scoped
    # (reverts on `engine.begin()` commit), so the existing lock-blocking
    # tests that set their own `statement_timeout='750ms'` on a contender
    # connection are unaffected. The helper uses `try/finally` so the
    # short-lived engine is disposed even on the setup-failure path.
    # Blast Radius: None detected — test-harness fixture only; no product
    # code, no migration, no `alembic/env.py` change.
    # Connections:
    #   - File: tests/_postgres_helpers.py -> `require_postgres_url()`
    #     supplies the disposable test DB URL.
    #   - File: tests/db/_pg_schema_helpers.py -> `reset_public_schema` is
    #     the shared schema-reset helper used by all 9 fixtures.
    #   - File: AGENTS.md -> "Professional Commenting Standard" (this block).
    # ============================================================================
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_source_account_migration_rekeys_uniqueness(
    alembic_config, fresh_engine
) -> None:
    """Upgrading creates the account-scoped key and tenant-month read index."""
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
    indexes = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_indexes("adsense_payments")
    }
    assert indexes["ix_adsense_payments_tenant_month_payment_date"] == (
        "tenant_id", "month", "payment_date",
    )
    checks = {c["name"] for c in inspector.get_check_constraints("adsense_payments")}
    assert "ck_adsense_payments_source_account_id_nonempty" in checks


# ============================================================================
# Purpose: Prove the upgrade()'s data-backfill safety path actually runs — a
#          legacy adsense_payments row that predates source_account_id (so its
#          new column is NULL) is rewritten to the '__legacy_manual__' sentinel
#          and survives the subsequent SET NOT NULL. The other two tests run
#          against an EMPTY table, where the backfill UPDATE touches zero rows
#          and SET NOT NULL trivially succeeds — so they would still pass even
#          if the backfill were deleted. This is the only test that guards it.
# Database/ORM: adsense_payments / AdSensePaymentORM (finance_models.py).
# Standards: Raw INSERT at the PRIOR head (20260527_0001) where the column does
#            not exist yet; every NOT NULL column without a server default is
#            supplied with a CHECK-satisfying value. No FK on this table, so any
#            UUID tenant_id is valid (no tenants seed needed). id / imported_at /
#            updated_at have server defaults and are omitted.
# Blast Radius: Test-only. Finance source-of-truth backfill correctness.
# ============================================================================
def test_source_account_migration_backfills_legacy_null_rows(
    alembic_config, fresh_engine
) -> None:
    """Backfills existing pre-live-pull rows with the legacy account sentinel."""
    # 1. Step to the PRIOR head: adsense_payments exists WITHOUT
    #    source_account_id and with the old (tenant_id, month, payment_name) key.
    command.upgrade(alembic_config, "20260527_0001")

    # 2. Insert one legacy row at the prior-head schema. source_account_id does
    #    not exist yet, so it is omitted — the next migration adds it as NULL on
    #    this existing row, which is exactly what the backfill must repair.
    with fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO adsense_payments "
                "(tenant_id, month, payment_name, payment_date, payment_amount, "
                "payment_currency, payment_status, raw_payload) "
                "VALUES (:tenant_id, :month, :payment_name, :payment_date, "
                ":payment_amount, :payment_currency, :payment_status, "
                "CAST(:raw_payload AS jsonb))"
            ),
            {
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "month": "2026-04",
                "payment_name": "Legacy Manual Upload",
                "payment_date": "2026-04-30",
                "payment_amount": "1234.560000",
                "payment_currency": "USD",
                "payment_status": "PAID",
                "raw_payload": "{}",
            },
        )

    # 3. Run 20260529_0001: add column (NULL on the legacy row) -> backfill ->
    #    SET NOT NULL + CHECK + re-key. The SET NOT NULL can only succeed if the
    #    backfill populated the legacy row first.
    command.upgrade(alembic_config, "head")

    # 4a. The backfill populated the legacy NULL with the sentinel.
    with fresh_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT source_account_id FROM adsense_payments "
                "WHERE payment_name = :payment_name"
            ),
            {"payment_name": "Legacy Manual Upload"},
        ).fetchall()
    # Row still exists (backfill did not drop it) and carries the sentinel.
    assert len(rows) == 1
    assert rows[0][0] == "__legacy_manual__"

    # 4b. The column ended NOT NULL with data present — SET NOT NULL succeeded.
    cols = {c["name"]: c for c in inspect(fresh_engine).get_columns("adsense_payments")}
    assert cols["source_account_id"]["nullable"] is False


def test_source_account_migration_downgrade_reverses(
    alembic_config, fresh_engine
) -> None:
    """Downgrading to the prior revision removes source-account schema changes."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260527_0001")
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


def test_source_account_migration_downgrade_fails_on_account_collisions(
    alembic_config, fresh_engine
) -> None:
    """Downgrade reports account-scoped duplicates before restoring old key."""
    command.upgrade(alembic_config, "head")
    with fresh_engine.begin() as conn:
        for source_account_id in ("pub-1", "pub-2"):
            conn.execute(
                text(
                    "INSERT INTO adsense_payments "
                    "(tenant_id, source_account_id, month, payment_name, "
                    "payment_date, payment_amount, payment_currency, "
                    "payment_status, raw_payload) "
                    "VALUES (:tenant_id, :source_account_id, :month, "
                    ":payment_name, :payment_date, :payment_amount, "
                    ":payment_currency, :payment_status, CAST(:raw_payload AS jsonb))"
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "source_account_id": source_account_id,
                    "month": "2026-04",
                    "payment_name": "accounts-collide-on-old-key",
                    "payment_date": "2026-04-30",
                    "payment_amount": "1234.560000",
                    "payment_currency": "USD",
                    "payment_status": "PAID",
                    "raw_payload": "{}",
                },
            )

    with pytest.raises(Exception, match="account-scoped duplicate AdSense payments"):
        command.downgrade(alembic_config, "20260527_0001")
