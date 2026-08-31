# ============================================================================
# Purpose: Prove the PR #228 schema revision preserves SQLite expression
#   indexes, mirrors ORM constraints, and emits the complete PostgreSQL RLS DDL.
# Database/ORM: users, external_identities, us_withholding_rate_configs.
# Standards: Direct Alembic execution, round-trip assertions, and recorded SQL.
# Blast Radius: Test-only coverage for authorization and finance schema safety.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260828_0001_external_identity_and_withholding.py -> Migration under test.
#   - File: tests/db/test_external_identity_withholding_migration_postgres.py
#     -> Live PostgreSQL behavior proof.
# ============================================================================
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260828_0001_external_identity_and_withholding.py"
)

_PRIOR_SCHEMA = """
CREATE TABLE org_units (
    id CHAR(32) NOT NULL,
    tenant_id CHAR(32) NOT NULL,
    name TEXT NOT NULL,
    CONSTRAINT pk_org_units PRIMARY KEY (id),
    CONSTRAINT uq_org_units_tenant_id_id UNIQUE (tenant_id, id)
);
CREATE TABLE users (
    id CHAR(32) NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    is_service_account BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tenant_id CHAR(32) NOT NULL,
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT ck_users_status CHECK (status IN ('active', 'disabled', 'service')),
    CONSTRAINT ck_users_service_account_status CHECK (
        (is_service_account = 1 AND status IN ('service', 'disabled'))
        OR (is_service_account = 0 AND status IN ('active', 'disabled'))
    )
);
CREATE UNIQUE INDEX uq_users_email_lower ON users (tenant_id, lower(email));
CREATE INDEX ix_users_tenant_id ON users (tenant_id);
"""


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m_20260828_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_migration(module: ModuleType, connection: sa.Connection) -> None:
    module.op = Operations(MigrationContext.configure(connection))


def _sqlite_object_sql(connection: sa.Connection, *, kind: str, name: str) -> str:
    sql = connection.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = :kind AND name = :name"),
        {"kind": kind, "name": name},
    ).scalar_one()
    assert isinstance(sql, str)
    return " ".join(sql.lower().split())


def _assert_case_variant_user_email_rejected(connection: sa.Connection) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(id, tenant_id, email, display_name) VALUES "
                "('00000000000000000000000000088112', "
                "'00000000000000000000000000000001', "
                "'PREEXISTING@EXAMPLE.COM', 'Duplicate')"
            )
        )


def _assert_existing_user_schema_preserved(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    index_names = set(
        connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'users'")
        ).scalars()
    )
    check_names = {constraint["name"] for constraint in inspector.get_check_constraints("users")}
    assert "ix_users_tenant_id" in index_names
    assert {"ck_users_status", "ck_users_service_account_status"} <= check_names


def _assert_integrity_error(
    connection: sa.Connection,
    statement: str,
    params: dict[str, object] | None = None,
) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(statement), params or {})
    finally:
        savepoint.rollback()


def _assert_new_sqlite_constraints_enforced(connection: sa.Connection) -> None:
    tenant_id = "00000000000000000000000000000001"
    user_id = "00000000000000000000000000088111"
    connection.execute(
        sa.text(
            "INSERT INTO external_identities "
            "(id, tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES ('00000000000000000000000000088201', :tenant_id, 'google', "
            "'subject-a', 'mapped@example.com', :user_id)"
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    _assert_integrity_error(
        connection,
        "INSERT INTO external_identities "
        "(id, tenant_id, provider, provider_subject, normalized_email, user_id) "
        "VALUES ('00000000000000000000000000088202', :tenant_id, 'google', "
        "'subject-a', 'other@example.com', :user_id)",
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    _assert_integrity_error(
        connection,
        "INSERT INTO external_identities "
        "(id, tenant_id, provider, provider_subject, normalized_email, user_id) "
        "VALUES ('00000000000000000000000000088203', :tenant_id, 'google', "
        "'subject-b', 'MAPPED@EXAMPLE.COM', :user_id)",
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    # Review P2: blank or whitespace-bearing claims must be unstorable in the
    # migrated schema itself, not only through the ORM.
    for row_id, provider, subject, email in (
        ("88212", "", "subject-c", "blank@example.com"),
        ("88213", "google", "", "blank@example.com"),
        ("88214", "google", "subject-c", ""),
        ("88215", " google", "subject-c", "padded@example.com"),
        ("88216", "google", " subject-c", "padded@example.com"),
        ("88217", "google", "subject-c", "padded@example.com "),
    ):
        _assert_integrity_error(
            connection,
            "INSERT INTO external_identities "
            "(id, tenant_id, provider, provider_subject, normalized_email, user_id) "
            f"VALUES ('0000000000000000000000000{row_id}', :tenant_id, :provider, "
            ":subject, :email, :user_id)",
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "provider": provider,
                "subject": subject,
                "email": email,
            },
        )

    connection.execute(
        sa.text(
            "INSERT INTO us_withholding_rate_configs "
            "(id, tenant_id, source_account_id, effective_from, revision, rate, "
            "account_type, confirmed_by_user_id) VALUES "
            "('00000000000000000000000000088301', :tenant_id, 'pub-pr228-a', "
            "'2026-01-01', 1, 0.15, 'business', :user_id)"
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    invalid_withholding_rows = (
        ("'pub-pr228-b', '2026-01-01', 1, -0.01, 'business'", "88302"),
        ("'pub-pr228-b', '2026-01-01', 1, 0.31, 'business'", "88303"),
        ("'pub-pr228-b', '2026-01-01', 1, 0.15, 'unknown'", "88304"),
        ("'pub-pr228-b', '2026-01-01', 0, 0.15, 'business'", "88305"),
        ("'   ', '2026-01-01', 1, 0.15, 'business'", "88306"),
        ("'pub-pr228-a', '2026-01-01', 1, 0.20, 'business'", "88307"),
        ("'accounts/pub-pr228-b', '2026-01-01', 1, 0.15, 'business'", "88308"),
        ("'pub?pr228-b', '2026-01-01', 1, 0.15, 'business'", "88309"),
        ("'pub#pr228-b', '2026-01-01', 1, 0.15, 'business'", "88310"),
        ("'pub%pr228-b', '2026-01-01', 1, 0.15, 'business'", "88311"),
    )
    for values, suffix in invalid_withholding_rows:
        _assert_integrity_error(
            connection,
            "INSERT INTO us_withholding_rate_configs "
            "(id, tenant_id, source_account_id, effective_from, revision, rate, "
            "account_type, confirmed_by_user_id) VALUES "
            f"('000000000000000000000000000{suffix}', :tenant_id, {values}, :user_id)",
            {"tenant_id": tenant_id, "user_id": user_id},
        )
    for offset, invalid_account in enumerate(("\t", "\n", "\r", "\f", "\v"), start=12):
        _assert_integrity_error(
            connection,
            "INSERT INTO us_withholding_rate_configs "
            "(id, tenant_id, source_account_id, effective_from, revision, rate, "
            "account_type, confirmed_by_user_id) VALUES "
            "(:id, :tenant_id, :invalid_account, '2026-01-01', 1, 0.15, "
            "'business', :user_id)",
            {
                "id": f"00000000000000000000000000088{offset}",
                "tenant_id": tenant_id,
                "invalid_account": invalid_account,
                "user_id": user_id,
            },
        )


def test_sqlite_upgrade_and_downgrade_preserve_user_email_expression_index() -> None:
    module = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        for statement in _PRIOR_SCHEMA.split(";"):
            if statement.strip():
                connection.execute(sa.text(statement))
        connection.execute(
            sa.text(
                "INSERT INTO users (id, tenant_id, email, display_name) VALUES "
                "('00000000000000000000000000088111', "
                "'00000000000000000000000000000001', "
                "'preexisting@example.com', 'Preexisting')"
            )
        )
        _bind_migration(module, connection)

        module.upgrade()

        inspector = sa.inspect(connection)
        assert {"external_identities", "us_withholding_rate_configs"} <= set(
            inspector.get_table_names()
        )
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        assert "home_org_unit_id" in user_columns
        user_fks = {
            foreign_key["name"]: foreign_key for foreign_key in inspector.get_foreign_keys("users")
        }
        home_fk = user_fks["fk_users_tenant_home_org_unit"]
        assert home_fk["constrained_columns"] == ["tenant_id", "home_org_unit_id"]
        assert home_fk["referred_table"] == "org_units"
        assert home_fk["referred_columns"] == ["tenant_id", "id"]
        assert (home_fk.get("options") or {}).get("ondelete") == "RESTRICT"
        external_fks = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("external_identities")
        }
        external_user_fk = external_fks["fk_external_identities_tenant_user"]
        assert external_user_fk["constrained_columns"] == ["tenant_id", "user_id"]
        assert external_user_fk["referred_columns"] == ["tenant_id", "id"]
        assert (external_user_fk.get("options") or {}).get("ondelete") == "CASCADE"
        withholding_fks = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("us_withholding_rate_configs")
        }
        confirmed_by_fk = withholding_fks["fk_us_withholding_rate_configs_confirmed_by"]
        assert confirmed_by_fk["constrained_columns"] == [
            "tenant_id",
            "confirmed_by_user_id",
        ]
        assert confirmed_by_fk["referred_columns"] == ["tenant_id", "id"]
        assert (confirmed_by_fk.get("options") or {}).get("ondelete") == "RESTRICT"
        assert "tenant_id, lower(email)" in _sqlite_object_sql(
            connection,
            kind="index",
            name="uq_users_email_lower",
        )
        rate_index_sql = _sqlite_object_sql(
            connection,
            kind="index",
            name="ix_us_withholding_rate_configs_tenant_effective",
        )
        assert "tenant_id, source_account_id, effective_from desc, revision desc" in rate_index_sql
        withholding_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("us_withholding_rate_configs")
        }
        assert withholding_uniques["uq_us_withholding_rate_configs_account_effective_revision"] == [
            "tenant_id",
            "source_account_id",
            "effective_from",
            "revision",
        ]
        for table in ("external_identities", "us_withholding_rate_configs"):
            tenant_column = next(
                column for column in inspector.get_columns(table) if column["name"] == "tenant_id"
            )
            assert tenant_column["default"] is None
        source_account_column = next(
            column
            for column in inspector.get_columns("us_withholding_rate_configs")
            if column["name"] == "source_account_id"
        )
        assert source_account_column["nullable"] is False
        assert source_account_column["default"] is None
        assert connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 1
        _assert_case_variant_user_email_rejected(connection)
        _assert_existing_user_schema_preserved(connection)
        _assert_new_sqlite_constraints_enforced(connection)

        module.downgrade()

        downgraded_inspector = sa.inspect(connection)
        assert "external_identities" not in downgraded_inspector.get_table_names()
        assert "us_withholding_rate_configs" not in downgraded_inspector.get_table_names()
        assert "home_org_unit_id" not in {
            column["name"] for column in downgraded_inspector.get_columns("users")
        }
        assert "tenant_id, lower(email)" in _sqlite_object_sql(
            connection,
            kind="index",
            name="uq_users_email_lower",
        )
        assert connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 1
        assert "ix_users_home_org_unit_id" not in set(
            connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'users'"
                )
            ).scalars()
        )
        _assert_case_variant_user_email_rejected(connection)
        _assert_existing_user_schema_preserved(connection)


class _RecordingDialect:
    name = "postgresql"


class _RecordingBind:
    dialect = _RecordingDialect()

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def test_postgres_rls_sql_contains_force_policy_checks_and_exact_grants() -> None:
    module = _load_migration()
    bind = _RecordingBind()

    module._configure_tenant_isolation(bind)

    statements = "\n".join(bind.statements)
    for table in ("external_identities", "us_withholding_rate_configs"):
        assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in statements
        assert f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY' in statements
        assert f"CREATE POLICY {table}_tenant_isolation" in statements
        assert f'REVOKE ALL ON public."{table}" FROM PUBLIC' in statements
    assert "USING (tenant_id = app_current_tenant_id())" in statements
    assert "WITH CHECK (tenant_id = app_current_tenant_id())" in statements
    assert 'GRANT SELECT ON public."external_identities" TO "app_tenant"' in statements
    assert 'GRANT SELECT ON public."us_withholding_rate_configs" TO "app_tenant"' in statements
    assert 'GRANT SELECT ON public."external_identities" TO "app_platform"' in statements
    assert 'GRANT SELECT ON public."us_withholding_rate_configs" TO "app_platform"' in statements
    assert "GRANT SELECT, INSERT" not in statements
