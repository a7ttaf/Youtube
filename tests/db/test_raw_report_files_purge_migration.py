"""PostgreSQL round-trip tests for Track F raw_report_files purge columns."""

from pathlib import Path
from uuid import uuid4

import pytest
from _postgres_helpers import require_postgres_url  # sibling via pytest prepend
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260608_0002"
PURGE_HEAD = "20260609_0001"


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
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        # Fail fast (don't hang) if a stray connection holds a public-schema
        # lock: without lock_timeout, DROP SCHEMA waits indefinitely.
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def _insert_tenant(conn, tenant_id, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (:id, :slug, :display_name, 'USD', 'ACTIVE')"
        ),
        {"id": tenant_id, "slug": slug, "display_name": slug.title()},
    )


def _insert_raw_file(conn, tenant_id, raw_file_id, *, status: str) -> None:
    conn.execute(
        text(
            "INSERT INTO raw_report_files "
            "(id, tenant_id, source, report_type, report_month, file_url, "
            "checksum, parse_status) "
            "VALUES (:id, :tenant_id, 'youtube-reporting', 'rt-a', "
            "'2026-04', 'gs://bucket/tenant/file.csv', :checksum, :status)"
        ),
        {
            "id": raw_file_id,
            "tenant_id": tenant_id,
            "checksum": uuid4().hex,
            "status": status,
        },
    )


def test_upgrade_adds_purge_columns_and_widens_check(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, PURGE_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("raw_report_files")}

    assert "purged_at" in columns
    assert "purged_by" in columns

    tenant_id = uuid4()
    raw_file_id = uuid4()
    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_id, "tenant-a")
        _insert_raw_file(conn, tenant_id, raw_file_id, status="PARSED")
        conn.execute(
            text(
                "UPDATE raw_report_files SET parse_status = 'PURGED', "
                "file_url = '', purged_at = now(), purged_by = :actor "
                "WHERE id = :id"
            ),
            {"id": raw_file_id, "actor": uuid4()},
        )
        status = conn.execute(
            text("SELECT parse_status FROM raw_report_files WHERE id = :id"),
            {"id": raw_file_id},
        ).scalar()
    assert status == "PURGED"


def test_downgrade_succeeds_when_no_purged_rows_then_upgrade_back(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, PURGE_HEAD)
    command.downgrade(alembic_config, PRIOR_HEAD)

    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("raw_report_files")}
    assert "purged_at" not in columns
    assert "purged_by" not in columns

    # Leave the DB at head for downstream PG-tier tests.
    command.upgrade(alembic_config, PURGE_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("raw_report_files")}
    assert "purged_at" in columns
    assert "purged_by" in columns
