"""PostgreSQL round-trip for 20260531_0001 (channel-account map tables)."""
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


def test_upgrade_creates_both_tables_with_constraints(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" in inspector.get_table_names()
    assert "content_owner_channel_links" in inspector.get_table_names()
    checks = {
        c["name"] for c in inspector.get_check_constraints("adsense_content_owner_links")
    }
    assert "ck_adsense_content_owner_links_status" in checks
    assert "ck_adsense_content_owner_links_range" in checks
    assert "ck_adsense_content_owner_links_provenance_payload_object" in checks
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("adsense_content_owner_links")
    }
    assert uniques["uq_adsense_content_owner_links_key"] == (
        "tenant_id", "adsense_account_id", "content_owner_id", "effective_month_start",
    )
    fks = {c["name"] for c in inspector.get_foreign_keys("content_owner_channel_links")}
    assert "fk_content_owner_channel_links_tenant" in fks


def test_downgrade_drops_both_tables(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" not in inspector.get_table_names()
    assert "content_owner_channel_links" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    command.upgrade(alembic_config, "head")
    assert "adsense_content_owner_links" in inspect(fresh_engine).get_table_names()


def test_provenance_payload_object_check_rejects_array(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO adsense_content_owner_links "
        "(tenant_id, adsense_account_id, content_owner_id, provenance_kind, "
        "provenance_payload, effective_month_start) VALUES "
        "(:tenant, 'pub-1', 'owner-1', 'OPERATOR_ASSERTED', '[]'::jsonb, '2026-01')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})
