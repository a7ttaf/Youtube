# ============================================================================
# Purpose: Prove 20260825_0002 is an irreversible PostgreSQL security floor.
#   A real Alembic downgrade by a non-superuser, NOBYPASSRLS schema/table owner
#   must refuse before touching data, RLS posture, or the version stamp.
# Database/ORM: alembic_version, user_role_assignments,
#   user_permission_grants, and PostgreSQL RLS catalogs.
# Standards: Disposable generated login/schema ownership; real Alembic command;
#   exact typed error, version, empty-data, and ENABLE/FORCE posture assertions.
# Blast Radius: Test-only disposable PostgreSQL schema and generated role.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0002_beta_operator_authorization_repair.py -> refused downgrade.
#   - File: tests/db/_pg_schema_helpers.py -> bounded disposable schema reset.
# ============================================================================
"""PostgreSQL regression for the irreversible authorization repair."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._pg_schema_helpers import reset_public_schema
from tests.db._postgres_helpers import require_postgres_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GUARDED_TABLES = ("user_role_assignments", "user_permission_grants")


def _alembic_config(url: str) -> Config:
    """Return an Alembic config bound to one explicit disposable database URL."""
    config = Config()
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic"),
    )
    return config


def _owner_url(admin_url: str, *, role_name: str, password: str) -> str:
    """Return the same database URL authenticated as the generated owner."""
    url = sa.engine.make_url(admin_url).set(username=role_name, password=password)
    return url.render_as_string(hide_password=False)


def _rls_posture(connection: sa.Connection) -> dict[str, tuple[bool, bool]]:
    """Return ENABLE/FORCE flags for the exact guarded authorization tables."""
    rows = connection.execute(
        sa.text(
            "SELECT relation.relname, relation.relrowsecurity, "
            "relation.relforcerowsecurity "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = :schema_name "
            "AND relation.relname IN :table_names"
        ).bindparams(sa.bindparam("table_names", expanding=True)),
        {"schema_name": "public", "table_names": list(_GUARDED_TABLES)},
    ).all()
    return {
        str(row.relname): (bool(row.relrowsecurity), bool(row.relforcerowsecurity)) for row in rows
    }


def _beta_permissions(connection: sa.Connection) -> set[str]:
    """Return the exact durable permission keys assigned to Beta Operator."""
    return set(
        connection.execute(
            sa.text(
                "SELECT permission_key FROM role_permission_assignments WHERE role_key = :role_key"
            ),
            {"role_key": "beta_operator"},
        ).scalars()
    )


def _drop_generated_owner(connection: sa.Connection, role_name: str) -> None:
    """Drop the exact generated role after its owned schema has been reset."""
    exists = connection.scalar(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
        {"role_name": role_name},
    )
    if exists is None:
        return
    connection.exec_driver_sql(f'DROP OWNED BY "{role_name}"')
    connection.exec_driver_sql(f'DROP ROLE "{role_name}"')


@pytest.fixture
def irreversible_owner_database() -> Iterator[tuple[str, sa.Engine, str]]:
    """Provide head schema owned at the relevant seams by a restricted login."""
    admin_url = require_postgres_url()
    admin_config = _alembic_config(admin_url)
    role_name = f"authz_irreversible_owner_{uuid4().hex[:16]}"
    password = f"AuthzFloor{uuid4().hex}"
    owner_url = _owner_url(admin_url, role_name=role_name, password=password)

    reset_public_schema(admin_url)
    command.upgrade(admin_config, "20260825_0002")
    admin_engine = sa.create_engine(admin_url)
    owner_engine: sa.Engine | None = None
    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(
                f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
            )
            connection.exec_driver_sql(f'ALTER SCHEMA public OWNER TO "{role_name}"')
            for table_name in (*_GUARDED_TABLES, "alembic_version"):
                connection.exec_driver_sql(
                    f'ALTER TABLE public."{table_name}" OWNER TO "{role_name}"'
                )
        owner_engine = sa.create_engine(owner_url)
        yield owner_url, admin_engine, role_name
    finally:
        if owner_engine is not None:
            owner_engine.dispose()
        reset_public_schema(admin_url)
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            _drop_generated_owner(connection, role_name)
        command.upgrade(admin_config, "head")
        admin_engine.dispose()


def test_non_superuser_owner_cannot_cross_irreversible_security_floor(
    irreversible_owner_database: tuple[str, sa.Engine, str],
) -> None:
    """Downgrade refuses on empty data without changing stamp or RLS posture."""
    owner_url, admin_engine, role_name = irreversible_owner_database
    required_posture = {table_name: (True, True) for table_name in _GUARDED_TABLES}

    owner_engine = sa.create_engine(owner_url)
    try:
        with owner_engine.connect() as connection:
            flags = connection.execute(
                sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
            assert flags == (False, False)
            owners = set(
                connection.execute(
                    sa.text(
                        "SELECT pg_get_userbyid(relation.relowner) "
                        "FROM pg_catalog.pg_class AS relation "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = :schema_name "
                        "AND relation.relname IN :table_names"
                    ).bindparams(sa.bindparam("table_names", expanding=True)),
                    {"schema_name": "public", "table_names": list(_GUARDED_TABLES)},
                ).scalars()
            )
            assert owners == {role_name}
    finally:
        owner_engine.dispose()

    with admin_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "20260825_0002"
        )
        assert _rls_posture(connection) == required_posture
        beta_permissions = _beta_permissions(connection)
        assert "finance.import_manual_revenue" in beta_permissions
        assert "connectors.run_jobs" not in beta_permissions
        assert connection.scalar(sa.text("SELECT count(*) FROM user_role_assignments")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM user_permission_grants")) == 0

    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(_alembic_config(owner_url), "20260825_0001")
    assert type(exc_info.value).__name__ == "IrreversibleAuthorizationRepairError"
    assert "irreversible security repair" in str(exc_info.value)
    assert "reset/redeploy" in str(exc_info.value)

    with admin_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "20260825_0002"
        )
        assert _rls_posture(connection) == required_posture
        beta_permissions = _beta_permissions(connection)
        assert "finance.import_manual_revenue" in beta_permissions
        assert "connectors.run_jobs" not in beta_permissions
        assert connection.scalar(sa.text("SELECT count(*) FROM user_role_assignments")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM user_permission_grants")) == 0


# ============================================================================
# Purpose: Prove the 20260911_0001 rollback gate's PostgreSQL-only branches —
#   row-security disable, bounded table lock, and ACTIVE count — refuse a real
#   Alembic downgrade while a live beta_operator assignment exists, and pass
#   once the row is revoked (the 0002 irreversibility floor then refuses).
# Database/ORM: Disposable public schema; users/access_scopes/
#   user_role_assignments rows inserted through the ORM.
# Standards: Real alembic command.downgrade; exact typed error names, version
#   stamp, and row-survival assertions.
# Blast Radius: Test-only disposable PostgreSQL schema.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260911_0001_beta_operator_rollback_gate.py -> refused downgrade.
# ============================================================================
@pytest.fixture
def gated_database() -> Iterator[sa.Engine]:
    """Provide a disposable database migrated to head (past the gate)."""
    admin_url = require_postgres_url()
    admin_config = _alembic_config(admin_url)
    reset_public_schema(admin_url)
    command.upgrade(admin_config, "head")
    engine = sa.create_engine(admin_url)
    try:
        yield engine
    finally:
        engine.dispose()
        reset_public_schema(admin_url)


# ============================================================================
# Purpose: Seed one beta_operator user_role_assignments row (plus its users
#   and access_scopes FK parents) for the rollback-gate test.
# Database/ORM: users, access_scopes, user_role_assignments via the ORM on a
#   disposable PostgreSQL schema; single committed transaction.
# Standards: revoked assignments carry revoked_by/revoked_at per the
#   ck_user_role_assignments_revocation invariant.
# Blast Radius: Test-only disposable schema.
# Connections:
#   - File: tests/db/test_security_role_permission_seed_migration_postgres.py
#     -> gate refusal test.
# ============================================================================
def _insert_beta_assignment(engine: sa.Engine, *, active: bool) -> None:
    """Insert one beta_operator assignment via the ORM on real PostgreSQL."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from ums_smart_revenue.db.security_models import (
        AccessScopeORM,
        UserORM,
        UserRoleAssignmentORM,
    )

    user_id = uuid4()
    scope_id = uuid4()
    with Session(engine) as session:
        session.add(UserORM(id=user_id, email="beta@example.com", display_name="Beta"))
        session.add(
            AccessScopeORM(id=scope_id, scope_type="global", scope_id=None, label="Global")
        )
        session.add(
            UserRoleAssignmentORM(
                id=uuid4(),
                user_id=user_id,
                role_key="beta_operator",
                scope_id=scope_id,
                active=active,
                # ck_user_role_assignments_revocation: an inactive row must
                # carry its revocation fields.
                revoked_by=None if active else user_id,
                revoked_at=None if active else datetime.now(UTC),
            )
        )
        session.commit()


def test_gate_refuses_postgres_downgrade_while_beta_assignment_is_live(
    gated_database: sa.Engine,
) -> None:
    """A live beta_operator row stops the real downgrade; revoking clears it."""
    engine = gated_database
    admin_url = require_postgres_url()
    _insert_beta_assignment(engine, active=True)

    with pytest.raises(Exception) as exc_info:
        command.downgrade(_alembic_config(admin_url), "20260825_0002")
    assert type(exc_info.value).__name__ == "LiveBetaOperatorAssignmentError"
    assert "active beta_operator assignment" in str(exc_info.value)

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "20260911_0001"
        )
        assert connection.scalar(
            sa.text(
                "SELECT count(*) FROM user_role_assignments "
                "WHERE role_key = 'beta_operator' AND active"
            )
        ) == 1

    # Once revoked, the gate's guard passes and the next refusal is the 0002
    # irreversible-repair floor — proving the gate itself did not block.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE user_role_assignments SET active = false, "
                "revoked_by = user_id, revoked_at = now()"
            )
        )
    with pytest.raises(Exception) as exc_info:
        command.downgrade(_alembic_config(admin_url), "20260825_0001")
    assert type(exc_info.value).__name__ == "IrreversibleAuthorizationRepairError"


# ============================================================================
# Purpose: Prove the gate's bounded LOCK TABLE fails as a typed verification
#   error — not a silent hang — when a concurrent writer holds the table.
# Database/ORM: user_role_assignments ACCESS EXCLUSIVE lock on a second
#   connection; disposable PostgreSQL schema.
# Standards: Real alembic command.downgrade; ~10s lock_timeout wait is
#   expected (the gate's own bound); asserts the non-privilege error class
#   and that the version stamp survives.
# Blast Radius: Test-only disposable PostgreSQL schema.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260911_0001_beta_operator_rollback_gate.py -> bounded lock + classifier.
# ============================================================================
def test_gate_lock_contention_surfaces_verification_error(
    gated_database: sa.Engine,
) -> None:
    """A conflicting table lock hits lock_timeout -> RollbackGateVerificationError."""
    engine = gated_database
    admin_url = require_postgres_url()
    with engine.connect() as blocker:
        blocker.execute(
            sa.text("LOCK TABLE user_role_assignments IN ACCESS EXCLUSIVE MODE")
        )
        # The gate's LOCK TABLE ... SHARE ROW EXCLUSIVE now waits on this
        # blocker; its own lock_timeout=10s converts the wait into a
        # LockNotAvailable the classifier reports as a verification error.
        with pytest.raises(Exception) as exc_info:
            command.downgrade(_alembic_config(admin_url), "20260825_0002")
        blocker.rollback()
    assert type(exc_info.value).__name__ == "RollbackGateVerificationError"
    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == "20260911_0001"


@pytest.fixture
def gated_restricted_owner_database() -> Iterator[tuple[str, sa.Engine]]:
    """Provide head schema owned by a NOBYPASSRLS login for gate 42501 coverage."""
    admin_url = require_postgres_url()
    admin_config = _alembic_config(admin_url)
    role_name = f"authz_gate_owner_{uuid4().hex[:16]}"
    password = f"GateOwner{uuid4().hex}"
    owner_url = _owner_url(admin_url, role_name=role_name, password=password)

    reset_public_schema(admin_url)
    command.upgrade(admin_config, "head")
    admin_engine = sa.create_engine(admin_url)
    owner_engine: sa.Engine | None = None
    try:
        with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                f'CREATE ROLE "{role_name}" LOGIN PASSWORD \'{password}\' '
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
            )
            connection.exec_driver_sql(f'ALTER SCHEMA public OWNER TO "{role_name}"')
            for table_name in (*_GUARDED_TABLES, "alembic_version"):
                connection.exec_driver_sql(
                    f'ALTER TABLE public."{table_name}" OWNER TO "{role_name}"'
                )
        owner_engine = sa.create_engine(owner_url)
        yield owner_url, admin_engine
    finally:
        if owner_engine is not None:
            owner_engine.dispose()
        reset_public_schema(admin_url)
        with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            _drop_generated_owner(connection, role_name)
        command.upgrade(admin_config, "head")
        admin_engine.dispose()


# ============================================================================
# Purpose: Prove the gate's 42501 classification — a NOBYPASSRLS owner under
#   FORCE ROW LEVEL SECURITY passes the lock step but cannot disable
#   row_security for the count read, so the refusal maps to
#   LiveBetaOperatorAssignmentError with privileged-rerun guidance.
# Database/ORM: user_role_assignments FORCE RLS (posture set by
#   20260825_0002); generated disposable role.
# Standards: Real alembic command.downgrade as the restricted owner; asserts
#   the privilege error class and that the version stamp survives.
# Blast Radius: Test-only disposable PostgreSQL schema + generated role.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260911_0001_beta_operator_rollback_gate.py -> SQLSTATE classifier.
# ============================================================================
def test_gate_privilege_denial_maps_to_assignment_error(
    gated_restricted_owner_database: tuple[str, sa.Engine],
) -> None:
    """FORCE-RLS + NOBYPASSRLS owner read fails 42501 -> LiveBetaOperatorAssignmentError."""
    owner_url, admin_engine = gated_restricted_owner_database
    with pytest.raises(Exception) as exc_info:
        command.downgrade(_alembic_config(owner_url), "20260825_0002")
    assert type(exc_info.value).__name__ == "LiveBetaOperatorAssignmentError"
    assert "row-security" in str(exc_info.value)
    with admin_engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == "20260911_0001"
