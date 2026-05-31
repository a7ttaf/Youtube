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
    """Return the PostgreSQL database URL for testing."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Create an Alembic Config pointed at the test Postgres URL and script location."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    """Provide a fresh engine with a clean public schema for each test."""
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
    uniques2 = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("content_owner_channel_links")
    }
    assert uniques2["uq_content_owner_channel_links_key"] == (
        "tenant_id", "content_owner_id", "youtube_channel_id", "effective_month_start",
    )
    indexes = {c["name"] for c in inspector.get_indexes("adsense_content_owner_links")} | {
        c["name"] for c in inspector.get_indexes("content_owner_channel_links")
    }
    assert "ix_adsense_content_owner_links_account_status" in indexes
    assert "ix_content_owner_channel_links_owner" in indexes


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


def test_owner_channel_provenance_kind_check_rejects_unknown(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO content_owner_channel_links "
        "(tenant_id, content_owner_id, youtube_channel_id, provenance_kind, "
        "effective_month_start) VALUES "
        "(:tenant, 'owner-1', 'chan-1', 'BOGUS', '2026-04')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})


def test_repo_verify_blocks_on_held_per_account_advisory_lock(alembic_config, fresh_engine):
    """Proof the repository verify path acquires the per-account advisory lock.

    Connection A holds pg_advisory_xact_lock for (tenant, "pub-lock"). A second
    Session runs the repository's verify_account_owner_link under a short
    statement_timeout; because verify acquires the SAME per-account lock, the
    lock wait is canceled and raises. Commenting out _acquire_account_owner_lock
    in verify makes this test fail (the contender would not block).
    """
    from uuid import UUID, uuid4

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
        _account_owner_lock_key,
    )
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)

    # Seed one UNVERIFIED link (committed so the contender Session can load it).
    with Session(fresh_engine) as setup:
        link_id = (
            SqlAlchemyChannelAccountLinkRepository(setup, tenant_id=tenant)
            .propose_account_owner_link(
                adsense_account_id="pub-lock", content_owner_id="owner-1",
                effective_month_start="2026-01", effective_month_end=None,
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )
            .id
        )
        setup.commit()

    key = _account_owner_lock_key(tenant, "pub-lock")
    holder = fresh_engine.connect()
    holder_txn = holder.begin()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        with Session(fresh_engine) as contender:
            # statement_timeout reliably cancels a blocked advisory-lock wait.
            contender.execute(text("SET statement_timeout = '750ms'"))
            repo = SqlAlchemyChannelAccountLinkRepository(contender, tenant_id=tenant)
            raised = False
            try:
                repo.verify_account_owner_link(link_id, verified_by=uuid4(), reason="x")
            except OperationalError:
                raised = True
            assert raised, "verify must block on the held per-account advisory lock"
    finally:
        holder_txn.rollback()
        holder.close()


def test_repo_verify_succeeds_uncontended_on_postgres(alembic_config, fresh_engine):
    """Happy path: the verify path runs end-to-end against live Postgres."""
    from uuid import UUID, uuid4

    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)
    with Session(fresh_engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=tenant)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-pg", content_owner_id="owner-pg",
            effective_month_start="2026-01", effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
        )
        out = repo.verify_account_owner_link(link.id, verified_by=uuid4(), reason="ok")
        session.commit()
    assert out.verification_status == "VERIFIED"
