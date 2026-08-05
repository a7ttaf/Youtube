"""Schema guard for the additive channel_groups.content_owner_id column.

Covers both the ORM mirror and the migration itself, mirroring
``test_channel_group_cms_id_migration.py``'s SQLite-execution pattern for the
predecessor ``cms_group_id`` migration this one builds on.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from ums_smart_revenue.db.org_models import ChannelGroupORM

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260805_0001_channel_group_content_owner.py"
)
# channel_groups as 20260803_0001 left it: the pre-state this migration alters.
PRIOR_CHANNEL_GROUPS_DDL = """
CREATE TABLE channel_groups (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    group_type TEXT NOT NULL,
    cms_group_id TEXT,
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    tenant_id TEXT NOT NULL,
    CONSTRAINT pk_channel_groups PRIMARY KEY (id),
    CONSTRAINT uq_channel_groups_tenant_id_cms_group_id UNIQUE (tenant_id, cms_group_id)
)
"""


def test_channel_group_orm_exposes_content_owner_id() -> None:
    column = ChannelGroupORM.__table__.columns["content_owner_id"]
    assert column.nullable is True


def _migration_module() -> ModuleType:
    """Load the migration module by path (it is not importable by name)."""
    spec = importlib.util.spec_from_file_location("m_20260805_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_operations(module: ModuleType, connection: Connection) -> None:
    """Point the migration module's ``op`` at this connection's Operations."""
    module.op = Operations(MigrationContext.configure(connection))


def _insert_group(connection: Connection, *, cms_group_id: str, content_owner_id: str) -> None:
    """Insert one channel_groups row carrying the given CMS key and owner."""
    connection.execute(
        text(
            "INSERT INTO channel_groups"
            " (id, name, group_type, active, created_at, updated_at, tenant_id,"
            "  cms_group_id, content_owner_id)"
            " VALUES (:id, 'group', 'CMS', 1, '2026-08-05', '2026-08-05',"
            "  'tenant-a', :cms_group_id, :content_owner_id)"
        ),
        {"id": str(uuid4()), "cms_group_id": cms_group_id, "content_owner_id": content_owner_id},
    )


def test_migration_upgrade_adds_nullable_content_owner_id_on_sqlite() -> None:
    """The upgrade must reach head on SQLite and persist the owner value."""
    module = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        connection.execute(text(PRIOR_CHANNEL_GROUPS_DDL))
        _bind_operations(module, connection)
        module.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("channel_groups")}
        assert "content_owner_id" in columns

        _insert_group(connection, cms_group_id="cms-key-1", content_owner_id="owner-a")
        row = connection.execute(
            text("SELECT content_owner_id FROM channel_groups WHERE cms_group_id = 'cms-key-1'")
        ).one()
        assert row.content_owner_id == "owner-a"


def test_migration_downgrade_runs_on_sqlite() -> None:
    """The reverse path drops the column cleanly."""
    module = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        connection.execute(text(PRIOR_CHANNEL_GROUPS_DDL))
        _bind_operations(module, connection)
        module.upgrade()
        module.downgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("channel_groups")}
        assert "content_owner_id" not in columns
