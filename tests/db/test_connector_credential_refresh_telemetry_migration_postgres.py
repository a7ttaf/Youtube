"""PostgreSQL round-trip tests for Part 2 connector credential telemetry columns."""

from pathlib import Path
from uuid import uuid4

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url  # sibling via pytest prepend
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260609_0002"
TELEMETRY_HEAD = "20260612_0001"
_NEW_COLUMNS = {
    "last_refresh_attempt_at",
    "token_expiry_at",
    "last_refresh_status",
    "last_refresh_error_class",
}


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
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_tenant(conn, tenant_id, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (:id, :slug, :display_name, 'USD', 'ACTIVE')"
        ),
        {"id": tenant_id, "slug": slug, "display_name": slug.title()},
    )


def _insert_credential(conn, tenant_id, credential_id) -> None:
    conn.execute(
        text(
            "INSERT INTO api_connector_credentials "
            "(id, tenant_id, connector_key, account_id, encrypted_secret_ref, status) "
            "VALUES (:id, :tenant_id, 'youtube_reporting', 'acct-1', "
            "'secret-manager://ums/yt/acct-1', 'active')"
        ),
        {"id": credential_id, "tenant_id": tenant_id},
    )


def test_upgrade_adds_telemetry_columns(alembic_config: Config, fresh_engine: object) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.issubset(columns)


def test_downgrade_then_upgrade_round_trip(alembic_config: Config, fresh_engine: object) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    command.downgrade(alembic_config, PRIOR_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.isdisjoint(columns)
    # Leave the DB at head for downstream PG-tier tests.
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.issubset(columns)


def test_last_refresh_status_check_positive_and_negative(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    tenant_id = uuid4()
    credential_id = uuid4()
    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_id, "tenant-a")
        _insert_credential(conn, tenant_id, credential_id)
        conn.execute(
            text(
                "UPDATE api_connector_credentials SET last_refresh_status = 'failed' WHERE id = :id"
            ),
            {"id": credential_id},
        )
        value = conn.execute(
            text("SELECT last_refresh_status FROM api_connector_credentials WHERE id = :id"),
            {"id": credential_id},
        ).scalar()
    assert value == "failed"
    with pytest.raises(DatabaseError), fresh_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE api_connector_credentials "
                "SET last_refresh_status = 'bogus' WHERE id = :id"
            ),
            {"id": credential_id},
        )
