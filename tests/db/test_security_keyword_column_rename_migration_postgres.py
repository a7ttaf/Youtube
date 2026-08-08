# ============================================================================
# Purpose: PostgreSQL round-trip proof for migration 20260808_0001 (rename of
#          roles.key -> role_key, permissions.key -> permission_key,
#          permissions.sensitive -> is_sensitive, audit_logs.sensitive ->
#          is_sensitive): old names exist at the prior head, the upgrade
#          renames in place preserving rows and re-pointing FK constraints,
#          the downgrade restores the old names, and the renamed bootstrap
#          mirror (security_schema.sql) + seed (security_seed.sql) apply
#          cleanly to a scratch schema.
# Database/ORM: roles, permissions, role_permission_assignments, audit_logs
#               via raw SQL + SQLAlchemy inspection against disposable
#               PostgreSQL; SecurityBase is intentionally not used so the test
#               observes the migrated schema, not ORM metadata.
# Standards: Fail-closed — missing UMS_TEST_DATABASE_URL raises via
#            require_postgres_url; no skips. Assertions read the live
#            information_schema/inspector state, never ORM guesses.
# Blast Radius: Test harness only. No production schema impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260808_0001_rename_security_keyword_columns.py -> migration under test.
#   - File: backend/ums_smart_revenue/db/security_schema.sql -> bootstrap mirror
#     applied in the scratch proof.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> seed applied in
#     the scratch proof.
# ============================================================================
"""PostgreSQL round-trip tests for the 20260808_0001 keyword-column rename."""

from pathlib import Path

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url  # sibling module via pytest prepend mode
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260805_0001"
RENAME_HEAD = "20260808_0001"
SCHEMA_SQL = REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "security_schema.sql"
SEED_SQL = REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "security_seed.sql"


@pytest.fixture
def postgres_url() -> str:
    """Return the fail-closed PostgreSQL URL required for this suite."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Build an Alembic config bound to the disposable test database."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str, alembic_config: Config) -> object:
    """Reset the schema and yield an engine, restoring versioned head after."""
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()
        # Restore a clean versioned head after every test: a test that stops
        # at PRIOR_HEAD (or one that leaves bootstrap tables with no
        # alembic_version row) would otherwise leak stale schema state to
        # PG-tier neighbours that upgrade without a schema reset (session
        # hook, tenant RLS), including early-stop `-x` runs. Same "leave the
        # DB at head" convention as test_tenant_rls_migration.py.
        reset_public_schema(postgres_url)
        command.upgrade(alembic_config, "head")


def _column_names(engine: object, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _execute_script(engine: object, script_path: Path) -> None:
    # ============================================================================
    # Purpose: Apply a multi-statement bootstrap SQL file to the scratch
    # schema, statement by statement, so the bootstrap mirror + seed pair is
    # executed exactly as a fresh operator psql run would execute it.
    # Database/ORM: Raw DDL/DML via exec_driver_sql; no ORM involvement.
    # Standards: Splits on the statement terminator at line ends; comment-only
    # and empty fragments are skipped. No parameter binding (file is static).
    # Blast Radius: None detected — scratch-schema test helper only.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/security_schema.sql -> DDL input.
    #   - File: backend/ums_smart_revenue/db/security_seed.sql -> DML input.
    # ============================================================================
    raw = script_path.read_text(encoding="utf-8")
    statements = [
        chunk.strip()
        for chunk in raw.split(";\n")
        if chunk.strip() and any(
            not line.strip().startswith("--") for line in chunk.strip().splitlines()
        )
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


def test_prior_head_still_uses_keyword_column_names(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Verify the prior head still exposes the original keyword column names."""
    command.upgrade(alembic_config, PRIOR_HEAD)

    assert "key" in _column_names(fresh_engine, "roles")
    assert "role_key" not in _column_names(fresh_engine, "roles")
    assert {"key", "sensitive"} <= _column_names(fresh_engine, "permissions")
    assert "permission_key" not in _column_names(fresh_engine, "permissions")
    assert "is_sensitive" not in _column_names(fresh_engine, "permissions")
    assert "sensitive" in _column_names(fresh_engine, "audit_logs")
    assert "is_sensitive" not in _column_names(fresh_engine, "audit_logs")


def test_upgrade_renames_columns_preserves_rows_and_fk_enforcement(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Verify the upgrade renames columns, keeps rows, and keeps FK checks."""
    command.upgrade(alembic_config, PRIOR_HEAD)

    with fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO roles (key, label, description) "
                "VALUES ('probe_role', 'Probe Role', 'Rename probe')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO permissions (key, label, sensitive) "
                "VALUES ('probe.view', 'Probe Permission', true)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO role_permission_assignments (role_key, permission_key) "
                "VALUES ('probe_role', 'probe.view')"
            )
        )

    command.upgrade(alembic_config, RENAME_HEAD)

    assert "role_key" in _column_names(fresh_engine, "roles")
    assert "key" not in _column_names(fresh_engine, "roles")
    assert {"permission_key", "is_sensitive"} <= _column_names(fresh_engine, "permissions")
    assert "key" not in _column_names(fresh_engine, "permissions")
    assert "sensitive" not in _column_names(fresh_engine, "permissions")
    assert "is_sensitive" in _column_names(fresh_engine, "audit_logs")
    assert "sensitive" not in _column_names(fresh_engine, "audit_logs")

    with fresh_engine.begin() as conn:
        role_row = conn.execute(
            text("SELECT role_key, label FROM roles WHERE role_key = 'probe_role'")
        ).one()
        permission_row = conn.execute(
            text(
                "SELECT permission_key, is_sensitive FROM permissions "
                "WHERE permission_key = 'probe.view'"
            )
        ).one()
        assignment_count = conn.execute(
            text(
                "SELECT count(*) FROM role_permission_assignments "
                "WHERE role_key = 'probe_role' AND permission_key = 'probe.view'"
            )
        ).scalar_one()

    assert role_row == ("probe_role", "Probe Role")
    assert permission_row == ("probe.view", True)
    assert assignment_count == 1

    # FK constraints followed the rename: a dangling reference is still rejected.
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO role_permission_assignments (role_key, permission_key) "
                "VALUES ('missing_role', 'probe.view')"
            )
        )


def test_downgrade_restores_keyword_column_names(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Verify the downgrade restores the original keyword column names."""
    command.upgrade(alembic_config, RENAME_HEAD)
    command.downgrade(alembic_config, PRIOR_HEAD)

    assert "key" in _column_names(fresh_engine, "roles")
    assert "role_key" not in _column_names(fresh_engine, "roles")
    assert {"key", "sensitive"} <= _column_names(fresh_engine, "permissions")
    assert "sensitive" in _column_names(fresh_engine, "audit_logs")


def test_bootstrap_mirror_and_seed_apply_with_renamed_columns(fresh_engine: object) -> None:
    """Verify the renamed bootstrap mirror and seed apply to a scratch schema."""
    # The fresh_engine teardown restores the versioned head afterwards: the
    # bootstrap application leaves tables but no alembic_version row, and
    # PG-tier neighbours that upgrade without a schema reset (session hook,
    # tenant RLS) would then replay revision 0001 into existing tables.
    _execute_script(fresh_engine, SCHEMA_SQL)
    _execute_script(fresh_engine, SEED_SQL)

    with fresh_engine.begin() as conn:
        roles_count = conn.execute(text("SELECT count(*) FROM roles")).scalar_one()
        permissions_count = conn.execute(
            text("SELECT count(*) FROM permissions")
        ).scalar_one()
        assignments_count = conn.execute(
            text("SELECT count(*) FROM role_permission_assignments")
        ).scalar_one()
        sensitive_column = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'permissions' AND column_name = 'is_sensitive'"
            )
        ).scalar_one()
        super_owner_role = conn.execute(
            text("SELECT count(*) FROM roles WHERE role_key = 'super_owner'")
        ).scalar_one()
        sensitive_flags = dict(
            conn.execute(
                text(
                    "SELECT permission_key, is_sensitive FROM permissions "
                    "WHERE permission_key IN ('analytics.view', 'finance.view_revenue')"
                )
            ).all()
        )

    # Catalog-size-agnostic proof: assert the seed's semantic anchors (known
    # role key, known sensitive/insensitive permission flags) rather than
    # exact row counts, so routine seed catalog changes do not break it.
    assert roles_count > 0
    assert permissions_count > 0
    assert assignments_count > 0
    assert super_owner_role == 1
    assert sensitive_flags == {"analytics.view": False, "finance.view_revenue": True}
    assert sensitive_column == 1
