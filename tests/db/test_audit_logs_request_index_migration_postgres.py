"""PostgreSQL round-trip tests for the audit_logs lifecycle-request index."""

from pathlib import Path

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url  # sibling module via pytest prepend mode
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260911_0001"
INDEX_HEAD = "20260913_0001"
INDEX_NAME = "ix_audit_logs_tenant_event_request"


@pytest.fixture
def postgres_url() -> str:
    """Return the disposable PostgreSQL URL or skip the module's tests."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Build an Alembic Config pointed at the disposable database."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str) -> object:
    # ============================================================================
    # Purpose: Provide a fresh SQLAlchemy engine with a clean `public` schema
    #   for one migration round-trip test (shared helper owns the reset).
    # Database/ORM: PostgreSQL `public` schema via `reset_public_schema`.
    # Standards: `SET LOCAL lock_timeout = '30s'` inside the helper fails a
    #   contended reset fast instead of hanging; try/finally disposes the
    #   engine on the setup-failure path.
    # Blast Radius: None detected — test-harness fixture only.
    # Connections:
    #   - File: tests/db/_pg_schema_helpers.py -> schema reset.
    #   - File: tests/_postgres_helpers.py -> require_postgres_url().
    # ============================================================================
    """Yield a fresh engine on a reset public schema for one round-trip test."""
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _index_columns(engine: object) -> list[str] | None:
    """Return the index's ordered column list, or None when absent."""
    with engine.connect() as connection:  # type: ignore[union-attr]
        row = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        ).scalar_one_or_none()
    if row is None:
        return None
    # indexdef ends with `... USING btree (col_a, col_b, col_c)`
    columns_sql = row.rsplit("(", maxsplit=1)[-1].rstrip(")")
    return [column.strip() for column in columns_sql.split(",")]


def test_upgrade_creates_lifecycle_request_index(
    alembic_config: Config, fresh_engine: object
) -> None:
    """20260913_0001 builds (tenant_id, event_type, request_id) on audit_logs."""
    command.upgrade(alembic_config, INDEX_HEAD)
    assert _index_columns(fresh_engine) == ["tenant_id", "event_type", "request_id"]


def test_downgrade_drops_lifecycle_request_index(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Rolling back to the prior head removes only the index."""
    command.upgrade(alembic_config, INDEX_HEAD)
    command.downgrade(alembic_config, PRIOR_HEAD)
    assert _index_columns(fresh_engine) is None
    # The table itself survives: the downgrade is schema-only, not data loss.
    with fresh_engine.connect() as connection:  # type: ignore[union-attr]
        present = connection.execute(text("SELECT to_regclass('public.audit_logs')")).scalar_one()
    assert present == "audit_logs"
