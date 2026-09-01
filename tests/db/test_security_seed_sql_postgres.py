# ============================================================================
# Purpose: Execute the raw security seed against PostgreSQL under a real
#   non-superuser role and a non-bootstrap tenant context. Proves the global
#   access scope uses the trusted backend tenant, converges idempotently, and
#   fails closed before catalog writes when that context is missing.
# Database/ORM: tenants, access_scopes, roles, permissions,
#   role_permission_assignments, user_role_assignments, and
#   user_permission_grants at Alembic head with FORCE RLS enabled.
# Standards: Disposable role and transaction-scoped rows; the generated role
#   name is internal, tenant values are bound parameters, and every test rolls
#   back its data while role grants are removed in fixture teardown.
# Blast Radius: Test-only authorization and tenant-isolation coverage. No
#   finance calculations, exports, or persistent tenant data.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> Executed subject.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260608_0001_tenant_rls_enforcement.py -> Trusted context and RLS policy.
#   - File: tests/db/_postgres_helpers.py -> Disposable PostgreSQL URL contract.
# ============================================================================
"""PostgreSQL behavior tests for the raw security seed's tenant scope."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_SQL_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/security_seed.sql"
BOOTSTRAP_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _alembic_config(url: str) -> Config:
    """Return an Alembic config bound to the disposable PostgreSQL database."""
    config = Config()
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option(
        "script_location",
        "backend/ums_smart_revenue/db/alembic",
    )
    return config


def _drop_test_role(connection: sa.Connection, role_name: str) -> None:
    """Remove one generated test role and its exact grants if it exists."""
    exists = connection.scalar(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
        {"role_name": role_name},
    )
    if exists is None:
        return
    connection.exec_driver_sql(f'REVOKE "app_platform" FROM "{role_name}"')
    connection.exec_driver_sql(f'DROP OWNED BY "{role_name}"')
    connection.exec_driver_sql(f'DROP ROLE "{role_name}"')


@pytest.fixture
def non_superuser_seed_role() -> Iterator[tuple[sa.Engine, str]]:
    """Provide a non-superuser role with tenant-lane and catalog-seed grants."""
    url = require_postgres_url()
    command.upgrade(_alembic_config(url), "head")
    engine = sa.create_engine(url)
    role_name = f"security_seed_test_{uuid4().hex[:16]}"

    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(
                f'CREATE ROLE "{role_name}" NOLOGIN NOSUPERUSER NOCREATEDB '
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
            )
            connection.exec_driver_sql(f'GRANT "app_platform" TO "{role_name}"')
            # The runtime app_platform lane deliberately has no catalog DML.
            # This narrow, throwaway grant models the privileged maintenance
            # role required by security_seed.sql without changing table owners.
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                f'roles, permissions, role_permission_assignments TO "{role_name}"'
            )
        yield engine, role_name
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            _drop_test_role(connection, role_name)
        engine.dispose()


def _insert_tenant(connection: sa.Connection, tenant_id: UUID) -> None:
    """Create a transaction-local active tenant using bound values."""
    suffix = tenant_id.hex[:16]
    connection.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (:tenant_id, :slug, :display_name, 'USD', 'ACTIVE')"
        ),
        {
            "tenant_id": tenant_id,
            "slug": f"security-seed-{suffix}",
            "display_name": f"Security Seed {suffix}",
        },
    )


def _execute_seed(connection: sa.Connection) -> None:
    """Execute the complete SQL seed on the current backend and transaction."""
    connection.exec_driver_sql(SEED_SQL_PATH.read_text(encoding="utf-8"))


def _catalog_counts(connection: sa.Connection) -> tuple[int, int, int]:
    """Return stable counts for the three platform security catalogs."""
    return (
        connection.scalar(sa.text("SELECT count(*) FROM roles")),
        connection.scalar(sa.text("SELECT count(*) FROM permissions")),
        connection.scalar(sa.text("SELECT count(*) FROM role_permission_assignments")),
    )


def _bootstrap_global_scopes(connection: sa.Connection) -> set[tuple[UUID, str | None]]:
    """Return the pre-existing UMS global scope identity and label set."""
    return set(
        connection.execute(
            sa.text(
                "SELECT id, label FROM access_scopes "
                "WHERE tenant_id = :tenant_id AND scope_type = 'global' "
                "AND scope_id IS NULL"
            ),
            {"tenant_id": BOOTSTRAP_TENANT_ID},
        ).all()
    )


def test_security_seed_uses_non_bootstrap_trusted_tenant_and_is_idempotent(
    non_superuser_seed_role: tuple[sa.Engine, str],
) -> None:
    """A non-superuser maintenance role seeds tenant B exactly once."""
    engine, role_name = non_superuser_seed_role
    tenant_id = uuid4()

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            bootstrap_scopes_before = _bootstrap_global_scopes(connection)
            _insert_tenant(connection, tenant_id)
            connection.exec_driver_sql(f'SET LOCAL ROLE "{role_name}"')

            role_flags = connection.execute(
                sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
            assert role_flags == (False, False)

            connection.execute(
                sa.text("SELECT set_app_current_tenant_id(CAST(:tenant_id AS uuid))"),
                {"tenant_id": str(tenant_id)},
            )
            assert connection.scalar(sa.text("SELECT app_current_tenant_id()")) == tenant_id

            _execute_seed(connection)
            first_scope = connection.execute(
                sa.text(
                    "SELECT id, tenant_id, label FROM access_scopes "
                    "WHERE scope_type = 'global' AND scope_id IS NULL"
                )
            ).one()
            first_counts = _catalog_counts(connection)
            assert first_scope.tenant_id == tenant_id
            assert first_scope.label == "Global"
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM role_permission_assignments "
                        "WHERE role_key = 'beta_operator' "
                        "AND permission_key = 'finance.import_manual_revenue'"
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM role_permission_assignments "
                        "WHERE role_key = 'beta_operator' "
                        "AND permission_key = 'connectors.run_jobs'"
                    )
                )
                == 0
            )

            _execute_seed(connection)
            second_scope = connection.execute(
                sa.text(
                    "SELECT id, tenant_id, label FROM access_scopes "
                    "WHERE scope_type = 'global' AND scope_id IS NULL"
                )
            ).one()
            assert second_scope == first_scope
            assert _catalog_counts(connection) == first_counts

            connection.exec_driver_sql("RESET ROLE")
            assert _bootstrap_global_scopes(connection) == bootstrap_scopes_before
        finally:
            transaction.rollback()


def test_security_seed_without_trusted_tenant_fails_before_catalog_writes(
    non_superuser_seed_role: tuple[sa.Engine, str],
) -> None:
    """Missing backend tenant context rejects the seed without partial writes."""
    engine, role_name = non_superuser_seed_role
    tenant_id = uuid4()

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            _insert_tenant(connection, tenant_id)
            catalog_counts_before = _catalog_counts(connection)
            connection.execute(sa.text("SELECT clear_app_current_tenant_id()"))
            connection.exec_driver_sql(f'SET LOCAL ROLE "{role_name}"')
            assert connection.scalar(sa.text("SELECT app_current_tenant_id()")) is None

            savepoint = connection.begin_nested()
            with pytest.raises(sa.exc.DBAPIError) as exc_info:
                _execute_seed(connection)
            savepoint.rollback()

            sqlstate = getattr(exc_info.value.orig, "sqlstate", None)
            assert sqlstate in {"23502", "42501"}
            connection.exec_driver_sql("RESET ROLE")
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM access_scopes WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                == 0
            )
            assert _catalog_counts(connection) == catalog_counts_before
        finally:
            transaction.rollback()
