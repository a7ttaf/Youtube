"""PostgreSQL round-trip tests for B2.3 connector run tracking."""

from pathlib import Path
from uuid import uuid4

import pytest
from _postgres_helpers import require_postgres_url  # sibling module via pytest prepend mode
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260523_0001"
B2_3_HEAD = "20260527_0001"


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
    # Purpose: Provide a fresh SQLAlchemy engine with a clean `public` schema
    # for one migration round-trip test. Bounded by
    # `SET LOCAL lock_timeout = '30s'` so a contended reset (e.g. an orphan
    # `idle in transaction` connection from a prior killed/hung run on a
    # shared/reused cluster) fails fast with a diagnosable `LockNotAvailable`
    # error instead of hanging indefinitely.
    # Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`.
    # Standards: `SET LOCAL` is transaction-scoped (reverts on
    # `engine.begin()` commit), so the existing lock-blocking tests that set
    # their own `statement_timeout='750ms'` on contender connections are
    # unaffected. The `try/finally` wrapper guarantees `engine.dispose()`
    # runs even when the schema reset raises — no leaked pool on the
    # contended-reset failure path.
    # Blast Radius: None detected — test-harness fixture only; no product
    # code, no migration, no `alembic/env.py` change.
    # Connections:
    #   - File: tests/_postgres_helpers.py -> `require_postgres_url()`
    #     supplies the disposable test DB URL.
    #   - File: AGENTS.md -> "Professional Commenting Standard" (this block).
    #   - File: tests/db/test_tenant_rls_migration.py -> `_drop_public_schema`
    #     is the reference try/finally engine-disposal pattern.
    # ============================================================================
    engine = create_engine(postgres_url)
    try:
        with engine.begin() as conn:
            # Fail fast (don't hang) if a stray connection holds a
            # public-schema lock: without lock_timeout, DROP SCHEMA waits
            # indefinitely.
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        yield engine
    finally:
        engine.dispose()


def test_pre_state_at_prior_head_has_no_connector_run_tables(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, PRIOR_HEAD)
    inspector = inspect(fresh_engine)

    assert "connector_runs" not in inspector.get_table_names()
    assert "connector_run_raw_files" not in inspector.get_table_names()
    raw_uniques = {
        c["name"] for c in inspector.get_unique_constraints("raw_report_files")
    }
    assert "uq_raw_report_files_tenant_id_id" not in raw_uniques


def test_upgrade_creates_tables_constraints_and_indexes(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    inspector = inspect(fresh_engine)
    tables = set(inspector.get_table_names())

    assert "connector_runs" in tables
    assert "connector_run_raw_files" in tables

    run_uniques = {c["name"] for c in inspector.get_unique_constraints("connector_runs")}
    join_uniques = {
        c["name"] for c in inspector.get_unique_constraints("connector_run_raw_files")
    }
    raw_uniques = {
        c["name"] for c in inspector.get_unique_constraints("raw_report_files")
    }
    run_checks = {c["name"] for c in inspector.get_check_constraints("connector_runs")}
    join_checks = {
        c["name"] for c in inspector.get_check_constraints("connector_run_raw_files")
    }
    run_indexes = {i["name"] for i in inspector.get_indexes("connector_runs")}
    join_indexes = {
        i["name"] for i in inspector.get_indexes("connector_run_raw_files")
    }

    assert "uq_connector_runs_tenant_id" in run_uniques
    assert "uq_connector_run_raw_files_run_file" in join_uniques
    assert "uq_raw_report_files_tenant_id_id" in raw_uniques
    assert "ck_connector_runs_report_month_format" in run_checks
    assert "ck_connector_runs_status" in run_checks
    assert "ck_connector_runs_error_summary_len" in run_checks
    assert "ck_connector_runs_finished_at_order" in run_checks
    assert "ck_connector_run_raw_files_ordering_index_non_negative" in join_checks
    assert "ix_connector_runs_tenant_connector_month" in run_indexes
    assert "ix_connector_runs_tenant_started" in run_indexes
    assert "ix_connector_run_raw_files_tenant_raw_file" in join_indexes


def test_cross_tenant_raw_file_link_rejected_by_composite_fk(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    tenant_a = uuid4()
    tenant_b = uuid4()
    run_id = uuid4()
    raw_file_id = uuid4()

    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_a, "tenant-a")
        _insert_tenant(conn, tenant_b, "tenant-b")
        _insert_connector_run(conn, tenant_a, run_id)
        _insert_raw_file(conn, tenant_b, raw_file_id)

    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connector_run_raw_files "
                "(id, tenant_id, connector_run_id, raw_report_file_id, ordering_index) "
                "VALUES (:id, :tenant_id, :run_id, :raw_file_id, 0)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_a,
                "run_id": run_id,
                "raw_file_id": raw_file_id,
            },
        )


def test_triggered_by_user_fk_rejects_user_from_different_tenant(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    tenant_a = uuid4()
    tenant_b = uuid4()
    user_id = uuid4()

    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_a, "tenant-a")
        _insert_tenant(conn, tenant_b, "tenant-b")
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, display_name) "
                "VALUES (:id, :tenant_id, 'user@example.test', 'Wrong Tenant')"
            ),
            {"id": user_id, "tenant_id": tenant_b},
        )

    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        _insert_connector_run(conn, tenant_a, uuid4(), triggered_by_user_id=user_id)


def test_report_month_check_rejects_nondigit_month(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    tenant_id = uuid4()

    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_id, "tenant-a")

    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        _insert_connector_run(conn, tenant_id, uuid4(), report_month="2026-0a")


def test_join_ordering_index_check_rejects_negative_values(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    tenant_id = uuid4()
    run_id = uuid4()
    raw_file_id = uuid4()

    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_id, "tenant-a")
        _insert_connector_run(conn, tenant_id, run_id)
        _insert_raw_file(conn, tenant_id, raw_file_id)

    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connector_run_raw_files "
                "(id, tenant_id, connector_run_id, raw_report_file_id, ordering_index) "
                "VALUES (:id, :tenant_id, :run_id, :raw_file_id, -1)"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "run_id": run_id,
                "raw_file_id": raw_file_id,
            },
        )


def test_downgrade_drops_b2_3_tables_and_raw_unique_only(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    command.downgrade(alembic_config, "-1")
    inspector = inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    raw_uniques = {
        c["name"] for c in inspector.get_unique_constraints("raw_report_files")
    }

    assert "connector_runs" not in tables
    assert "connector_run_raw_files" not in tables
    assert "raw_report_files" in tables
    assert "google_revenue_source_rows" in tables
    assert "uq_raw_report_files_tenant_id_id" not in raw_uniques


def test_round_trip_idempotency(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, B2_3_HEAD)
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, B2_3_HEAD)

    inspector = inspect(fresh_engine)
    assert "connector_runs" in inspector.get_table_names()
    assert "connector_run_raw_files" in inspector.get_table_names()


def _insert_tenant(conn, tenant_id, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (:id, :slug, :display_name, 'USD', 'ACTIVE')"
        ),
        {"id": tenant_id, "slug": slug, "display_name": slug.title()},
    )


def _insert_connector_run(
    conn,
    tenant_id,
    run_id,
    *,
    report_month="2026-04",
    triggered_by_user_id=None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO connector_runs "
            "(id, tenant_id, connector_key, account_id, report_month, "
            "triggered_by_user_id, counts_json) "
            "VALUES (:id, :tenant_id, 'youtube-reporting', 'acct-test', "
            ":report_month, :triggered_by_user_id, '{}')"
        ),
        {
            "id": run_id,
            "tenant_id": tenant_id,
            "report_month": report_month,
            "triggered_by_user_id": triggered_by_user_id,
        },
    )


def _insert_raw_file(conn, tenant_id, raw_file_id) -> None:
    conn.execute(
        text(
            "INSERT INTO raw_report_files "
            "(id, tenant_id, source, report_type, report_month, file_url, "
            "checksum, parse_status) "
            "VALUES (:id, :tenant_id, 'youtube-reporting', 'rt-a', "
            "'2026-04', 'gs://bucket/tenant/file.csv', :checksum, 'PARSED')"
        ),
        {"id": raw_file_id, "tenant_id": tenant_id, "checksum": uuid4().hex},
    )
