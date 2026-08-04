"""Schema guard for the additive channel_groups.cms_group_id column.

Covers both the ORM mirror and the migration itself. The migration is executed
against SQLite because that is the local/disposable database state: a direct
``op.create_unique_constraint`` raises ``NotImplementedError`` there ("No
support for ALTER of constraints in SQLite dialect"), so the upgrade has to go
through batch mode to reach head anywhere but PostgreSQL (review #159
r3715427823).
"""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.db.org_models import ChannelGroupORM

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260803_0001_channel_group_cms_id.py"
)
UNIQUE_CONSTRAINT = "uq_channel_groups_tenant_id_cms_group_id"
# channel_groups as 20260510_0002 created it and 20260517_0001 re-keyed it:
# the pre-state this migration has to alter.
PRIOR_CHANNEL_GROUPS_DDL = """
CREATE TABLE channel_groups (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    group_type TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    tenant_id TEXT NOT NULL,
    CONSTRAINT pk_channel_groups PRIMARY KEY (id)
)
"""


def test_channel_group_orm_exposes_cms_group_id() -> None:
    column = ChannelGroupORM.__table__.columns["cms_group_id"]
    assert column.nullable is True


def test_channel_group_cms_id_is_unique_per_tenant() -> None:
    constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in ChannelGroupORM.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "cms_group_id") in constraints


def _migration_module():
    """Load the migration module by path (it is not importable by name)."""
    spec = importlib.util.spec_from_file_location("m_20260803_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_group(connection, *, cms_group_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO channel_groups"
            " (id, name, group_type, active, created_at, updated_at, tenant_id,"
            "  cms_group_id)"
            " VALUES (:id, 'group', 'CMS', 1, '2026-08-03', '2026-08-03',"
            "  'tenant-a', :cms_group_id)"
        ),
        {"id": str(uuid4()), "cms_group_id": cms_group_id},
    )


def test_migration_upgrade_runs_on_sqlite_and_enforces_the_unique_key() -> None:
    """The upgrade must reach head on SQLite, not only on PostgreSQL.

    SQLite cannot ALTER a constraint, so a direct create_unique_constraint
    aborts the migration before head on every local/disposable database.
    """
    module = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        connection.execute(text(PRIOR_CHANNEL_GROUPS_DDL))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("channel_groups")}
        assert "cms_group_id" in columns
        unique_names = {
            constraint["name"]
            for constraint in inspect(connection).get_unique_constraints("channel_groups")
        }
        assert UNIQUE_CONSTRAINT in unique_names

        _insert_group(connection, cms_group_id="cms-key-1")
        with pytest.raises(IntegrityError):
            _insert_group(connection, cms_group_id="cms-key-1")


def test_migration_downgrade_runs_on_sqlite() -> None:
    """The reverse path has the same SQLite constraint-ALTER limitation."""
    module = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        connection.execute(text(PRIOR_CHANNEL_GROUPS_DDL))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        module.downgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("channel_groups")}
        assert "cms_group_id" not in columns
