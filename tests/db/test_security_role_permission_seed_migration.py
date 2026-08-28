# ============================================================================
# Purpose: Guard the P0.7 seed at three levels — the migration actually writes
#   the catalog, it is idempotent and self-healing, and the three sources of
#   truth (auth/roles.py, auth/permissions.py, auth/seed.py) cannot drift from
#   the raw db/security_seed.sql twin that operators may still run by hand.
# Database/ORM: RoleORM, PermissionORM, RolePermissionAssignmentORM,
#   UserRoleAssignmentORM, UserPermissionGrantORM — exercised against a real
#   migrated SQLite database, not against ORM metadata.
# Standards: The migration module is loaded by path (Alembic revisions are not
#   importable by name) and bound to a live connection's Operations, the same
#   pattern test_channel_group_content_owner_migration.py uses.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> subject.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> the raw-SQL twin.
#   - File: Docs/20_DEPLOYMENT_READINESS_AUDIT.md -> H1.
# ============================================================================
"""Guards for the roles/permissions seed migration and its SQL twin."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS, Permission
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS, RoleKey
from ums_smart_revenue.auth.seed import initial_role_permission_rows
from ums_smart_revenue.db.frozen_security_catalog import (
    FROZEN_PERMISSION_ROWS,
    FROZEN_ROLE_PERMISSION_ROWS,
    FROZEN_ROLE_ROWS,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    PermissionORM,
    RoleORM,
    RolePermissionAssignmentORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260825_0001_security_role_permission_seed.py"
)
SEED_SQL_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/security_seed.sql"

_ROLE_ROW = re.compile(r"^\s*\('([^']*)', '([^']*)', '([^']*)', (true|false)\),?$", re.MULTILINE)
_PERMISSION_ROW = re.compile(
    r"^\s*\('([^']*)', '([^']*)', (true|false), (true|false)\),?$", re.MULTILINE
)
_PAIR_ROW = re.compile(r"^\s*\('([^']*)', '([^']*)'\),?$", re.MULTILINE)


def _migration_module() -> ModuleType:
    """Load the migration module by path (it is not importable by name)."""
    spec = importlib.util.spec_from_file_location("m_20260825_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_operations(module: ModuleType, connection: Connection) -> None:
    """Point the migration module's ``op`` at this connection's Operations."""
    module.op = Operations(MigrationContext.configure(connection))


def _security_engine() -> Engine:
    """Return an in-memory SQLite engine carrying the security schema."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    return engine


def _stored_roles(connection: Connection) -> dict[str, tuple[str, str, bool]]:
    """Return every stored role row keyed by role key."""
    rows = connection.execute(
        select(RoleORM.key, RoleORM.label, RoleORM.description, RoleORM.service_only)
    ).all()
    return {row.key: (row.label, row.description, bool(row.service_only)) for row in rows}


def _stored_permissions(connection: Connection) -> dict[str, tuple[str, bool, bool]]:
    """Return every stored permission row keyed by permission key."""
    rows = connection.execute(
        select(
            PermissionORM.key,
            PermissionORM.label,
            PermissionORM.sensitive,
            PermissionORM.audit_on_use,
        )
    ).all()
    return {row.key: (row.label, bool(row.sensitive), bool(row.audit_on_use)) for row in rows}


def _stored_pairs(connection: Connection) -> set[tuple[str, str]]:
    """Return every stored (role, permission) catalog edge."""
    rows = connection.execute(
        select(
            RolePermissionAssignmentORM.role_key,
            RolePermissionAssignmentORM.permission_key,
        )
    ).all()
    return {(row.role_key, row.permission_key) for row in rows}


def _expected_pairs() -> set[tuple[str, str]]:
    """Return the (role, permission) edges ``auth/seed.py`` declares."""
    return {(row["role"], row["permission"]) for row in initial_role_permission_rows()}


def _table_counts(connection: Connection) -> dict[str, int]:
    """Return the row count of every table in the security schema."""
    return {
        table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
        for table in SecurityBase.metadata.sorted_tables
    }


# The tables this revision is allowed to write. Named rather than counted on
# purpose: the ROW COUNT moves whenever a permission is added, but the TABLE SET
# is a design decision — and it is the half that couples to the backup gate.
_SEEDED_TABLES = {"roles", "permissions", "role_permission_assignments"}


def test_migration_writes_only_the_catalog_tables() -> None:
    """The revision seeds exactly three tables, and each one is a backup seed table.

    This is the coupling that already bit once, which is why this test measures
    rather than assumes. ``scripts/backup_database.py`` judges a dump partly by
    whether every table OUTSIDE its own ``SEED_TABLES`` is empty: that emptiness
    is how a first run tells a healthy database from one that was wiped and
    re-migrated, and it is a fail-closed refusal with no override. When this
    revision landed, ``SEED_TABLES`` was ``(alembic_version, currencies,
    tenants)`` — so the 148 rows seeded below read as application data, a virgin
    database measured ``non_seed_rows=148``, and the refusal stopped firing. A
    data-seeding migration had disabled a safety check in a file it never
    mentions. ``SEED_TABLES`` now lists all three names.

    So widening ``_SEEDED_TABLES`` below is not a local change: add the table to
    ``SEED_TABLES`` and re-measure the backup gate's virgin baseline in the same
    commit. This test is the near half of that pair —
    ``tests/scripts/test_backup_content_gate.py`` owns the far half and derives
    its expectation by parsing the migrations.
    """
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        before = _table_counts(connection)
        _bind_operations(module, connection)
        module.upgrade()
        after = _table_counts(connection)

        assert set(before) == set(after)
        written = {name for name, count in after.items() if count > before[name]}
        assert written == _SEEDED_TABLES
        # Derived from the registries, never typed as a literal: this is the row
        # total a virgin database now carries outside the backup gate's seed
        # tables, and it moves whenever a role, permission or edge is added.
        assert sum(after[name] - before[name] for name in written) == (
            len(ROLE_DEFINITIONS)
            + len(PERMISSION_DEFINITIONS)
            + len(initial_role_permission_rows())
        )


def test_migration_seeds_the_whole_catalog_on_a_fresh_database() -> None:
    """A fresh upgrade writes every role, permission, and catalog edge."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()

        roles = _stored_roles(connection)
        assert set(roles) == {role.value for role in RoleKey}
        for role, definition in ROLE_DEFINITIONS.items():
            assert roles[role.value] == (
                definition.label,
                definition.description,
                definition.service_only,
            )

        permissions = _stored_permissions(connection)
        # Derived from the enum on purpose. An earlier revision of this file also
        # asserted `len(permissions) == 26`; that literal is fully implied by the
        # set equality above and only adds a second place to edit when a
        # permission is added, so it is gone rather than kept in sync by hand.
        assert set(permissions) == {permission.value for permission in Permission}
        for permission, definition in PERMISSION_DEFINITIONS.items():
            assert permissions[permission.value] == (
                definition.label,
                definition.sensitive,
                definition.audit_on_use,
            )

        assert _stored_pairs(connection) == _expected_pairs()


def test_migration_upgrade_is_idempotent() -> None:
    """Running the upgrade twice inserts nothing new and raises nothing."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        first = (_stored_roles(connection), _stored_permissions(connection))
        first_pairs = _stored_pairs(connection)

        module.upgrade()

        assert (_stored_roles(connection), _stored_permissions(connection)) == first
        assert _stored_pairs(connection) == first_pairs


def test_migration_refreshes_stale_catalog_metadata() -> None:
    """A pre-existing row with drifted metadata converges to the registry values."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO roles (key, label, description, service_only) "
                "VALUES ('finance_admin', 'stale label', 'stale description', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO permissions (key, label, sensitive, audit_on_use) "
                "VALUES ('finance.view_revenue', 'stale label', 0, 0)"
            )
        )
        _bind_operations(module, connection)
        module.upgrade()

        role_definition = ROLE_DEFINITIONS[RoleKey.FINANCE_ADMIN]
        assert _stored_roles(connection)["finance_admin"] == (
            role_definition.label,
            role_definition.description,
            role_definition.service_only,
        )
        # The refresh must restore the sensitive/audit flags, never soften them.
        assert _stored_permissions(connection)["finance.view_revenue"] == (
            PERMISSION_DEFINITIONS[Permission.VIEW_REVENUE].label,
            True,
            True,
        )


def test_migration_downgrade_preserves_the_seeded_catalog() -> None:
    """Downgrade is non-destructive: the catalog seed is not reversed."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        after_upgrade_roles = _stored_roles(connection)
        after_upgrade_permissions = _stored_permissions(connection)
        after_upgrade_pairs = _stored_pairs(connection)
        module.downgrade()

        assert _stored_roles(connection) == after_upgrade_roles
        assert _stored_permissions(connection) == after_upgrade_permissions
        assert _stored_pairs(connection) == after_upgrade_pairs
        assert after_upgrade_pairs == _expected_pairs()


def test_migration_downgrade_preserves_preexisting_security_seed_catalog() -> None:
    """A SQL-preseeded catalog survives upgrade+downgrade (the P1 case)."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        # Simulate security_seed.sql having already populated the catalog.
        module.upgrade()
        pre_roles = _stored_roles(connection)
        pre_permissions = _stored_permissions(connection)
        pre_pairs = _stored_pairs(connection)
        # Second upgrade is metadata-refresh only; downgrade must not wipe.
        module.upgrade()
        module.downgrade()

        assert _stored_roles(connection) == pre_roles
        assert _stored_permissions(connection) == pre_permissions
        assert _stored_pairs(connection) == pre_pairs


def test_migration_downgrade_keeps_rows_a_live_assignment_still_needs() -> None:
    """Live grants remain valid because the catalog is not wiped on downgrade."""
    module = _migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        pairs_before = _stored_pairs(connection)

    user_id = uuid4()
    scope_id = uuid4()
    with Session(engine) as session:
        session.add(UserORM(id=user_id, email="ops@example.com", display_name="Ops"))
        session.add(AccessScopeORM(id=scope_id, scope_type="global", scope_id=None, label="Global"))
        session.add(
            UserRoleAssignmentORM(
                id=uuid4(),
                user_id=user_id,
                role_key="finance_admin",
                scope_id=scope_id,
                active=True,
            )
        )
        session.add(
            UserPermissionGrantORM(
                id=uuid4(),
                user_id=user_id,
                permission_key="finance.view_revenue",
                scope_id=scope_id,
                active=True,
            )
        )
        session.commit()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.downgrade()

        assert "finance_admin" in _stored_roles(connection)
        assert "finance.view_revenue" in _stored_permissions(connection)
        assert _stored_pairs(connection) == pairs_before
        assert ("finance_admin", "finance.view_revenue") in _stored_pairs(connection)

def test_frozen_security_catalog_matches_live_registries() -> None:
    """The migration snapshot must match today's registries until a new revision."""
    module = _migration_module()
    assert module.role_seed_rows() == [
        {
            "key": role.value,
            "label": definition.label,
            "description": definition.description,
            "service_only": definition.service_only,
        }
        for role, definition in sorted(ROLE_DEFINITIONS.items(), key=lambda item: item[0].value)
    ]
    assert FROZEN_ROLE_ROWS == module.role_seed_rows()
    assert FROZEN_PERMISSION_ROWS == module.permission_seed_rows()
    assert FROZEN_ROLE_PERMISSION_ROWS == module.role_permission_seed_rows()


def _values_block(sql: str, header: str, *, last: bool = False) -> str:
    """Return the VALUES body of the INSERT introduced by ``header``."""
    start = sql.rindex(header) if last else sql.index(header)
    values_at = sql.index("VALUES", start)
    end = sql.index("ON CONFLICT", values_at)
    return sql[values_at + len("VALUES") : end]


def test_security_seed_sql_matches_the_python_registries() -> None:
    """The raw SQL twin declares exactly the catalog the Python registries do.

    ``security_seed.sql`` is still the documented raw-reseed path, so it must not
    drift from the registries the migration now seeds from. This is the test the
    audit found missing: nothing pinned the SQL file to ``auth/permissions.py``.
    """
    sql = SEED_SQL_PATH.read_text(encoding="utf-8")

    role_block = _values_block(sql, "INSERT INTO roles (key, label, description, service_only)")
    sql_roles = {
        key: (label, description, service_only == "true")
        for key, label, description, service_only in _ROLE_ROW.findall(role_block)
    }
    assert sql_roles == {
        role.value: (definition.label, definition.description, definition.service_only)
        for role, definition in ROLE_DEFINITIONS.items()
    }

    permission_block = _values_block(
        sql, "INSERT INTO permissions (key, label, sensitive, audit_on_use)"
    )
    sql_permissions = {
        key: (label, sensitive == "true", audit_on_use == "true")
        for key, label, sensitive, audit_on_use in _PERMISSION_ROW.findall(permission_block)
    }
    assert sql_permissions == {
        permission.value: (definition.label, definition.sensitive, definition.audit_on_use)
        for permission, definition in PERMISSION_DEFINITIONS.items()
    }

    pair_block = _values_block(
        sql,
        "INSERT INTO role_permission_assignments (role_key, permission_key)",
        last=True,
    )
    explicit_pairs = set(_PAIR_ROW.findall(pair_block))
    # The SQL file grants super_owner every permission with a SELECT rather than
    # an explicit tuple per permission, so re-add that implicit fan-out here.
    super_owner_pairs = {
        (RoleKey.SUPER_OWNER.value, permission.value) for permission in PERMISSION_DEFINITIONS
    }
    assert explicit_pairs | super_owner_pairs == _expected_pairs()
    assert not (explicit_pairs & super_owner_pairs), (
        "security_seed.sql lists a super_owner pair explicitly AND via the "
        "SELECT fan-out; one of the two is now redundant."
    )
