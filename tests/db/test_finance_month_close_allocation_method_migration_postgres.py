"""PostgreSQL migration coverage for finance_month_close allocation_method."""

from pathlib import Path

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260612_0002"
ALLOCATION_METHOD_HEAD = "20260618_0001"


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
def fresh_engine(postgres_url: str) -> object:
    # ============================================================================
    # Purpose: Provide a fresh PostgreSQL schema for finance_month_close migration
    #   tests without reusing rows or constraints from earlier test cases.
    # Database/ORM: PostgreSQL public schema via reset_public_schema, then raw
    #   SQLAlchemy statements against finance_month_close.
    # Standards: Reuses the shared reset helper with lock-timeout protection and
    #   disposes the short-lived engine after each test.
    # Blast Radius: Test harness only. No product code, tenant policy, audit, or
    #   finance calculation behavior changes.
    # Connections:
    #   - File: tests/db/_postgres_helpers.py -> disposable Postgres URL contract.
    #   - File: tests/db/_pg_schema_helpers.py -> shared schema reset.
    # ============================================================================
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_close_sql(
    *, month: str = "2026-04", method: str | None = "gross_revenue_proportional"
) -> tuple[object, dict[str, object]]:
    return (
        text(
            "INSERT INTO finance_month_close "
            "(month, status, allocation_method, allocation_rule_payload) "
            "VALUES (:month, 'OPEN', :method, '{}'::jsonb)"
        ),
        {"month": month, "method": method},
    )


def test_upgrade_refuses_existing_unknown_allocation_method(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Pre-existing unknown methods must be cleaned before adding the CHECK."""
    command.upgrade(alembic_config, PRIOR_HEAD)
    sql, params = _insert_close_sql(method="legacy_custom_method")
    with fresh_engine.begin() as conn:
        conn.execute(sql, params)

    with pytest.raises(RuntimeError, match="outside the shared allowlist"):
        command.upgrade(alembic_config, ALLOCATION_METHOD_HEAD)


def test_head_check_accepts_allowlist_and_null_rejects_unknown(
    alembic_config: Config, fresh_engine: object
) -> None:
    """At head, finance_month_close allows NULL/five methods and rejects unknown."""
    command.upgrade(alembic_config, "head")
    allowed = (
        None,
        "gross_revenue_proportional",
        "post_tax_revenue_proportional",
        "company_level",
        "manual",
        "no_allocation",
    )
    with fresh_engine.begin() as conn:
        for index, method in enumerate(allowed, start=1):
            sql, params = _insert_close_sql(month=f"2026-{index:02d}", method=method)
            conn.execute(sql, params)

    bad_sql, bad_params = _insert_close_sql(
        month="2026-12",
        method="definitely_not_a_method",
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(bad_sql, bad_params)
