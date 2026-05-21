import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Connection

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_NAME = "20260518_0001_tenant_scoped_youtube_channel_identity"
MIGRATION_PATH = (
    PROJECT_ROOT / f"backend/ums_smart_revenue/db/alembic/versions/{MIGRATION_NAME}.py"
)


def test_migration_scopes_youtube_channel_identity_to_tenant():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_pre_migration_schema(connection)
        migration = _load_migration()

        _execute_migration(connection, migration, "upgrade")

        inspector = inspect(connection)
        youtube_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("youtube_channels")
        }
        monthly_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("monthly_channel_revenue_facts")
        }
        override_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("revenue_manual_overrides")
        }

        connection.execute(
            text(
                "INSERT INTO youtube_channels (id, tenant_id, youtube_channel_id)"
                " VALUES ('yt-a', 'tenant-a', 'shared-channel')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO youtube_channels (id, tenant_id, youtube_channel_id)"
                " VALUES ('yt-b', 'tenant-b', 'shared-channel')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO monthly_channel_revenue_facts"
                " (id, tenant_id, youtube_channel_id, month, source_kind)"
                " VALUES ('fact-a', 'tenant-a', 'shared-channel', '2026-03',"
                " 'YOUTUBE_CMS')"
            )
        )

    assert youtube_uniques["uq_youtube_channels_tenant_channel_id"] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert "uq_youtube_channels_youtube_channel_id" not in youtube_uniques
    assert monthly_fks["fk_monthly_channel_revenue_facts_tenant_channel"][
        "constrained_columns"
    ] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert monthly_fks["fk_monthly_channel_revenue_facts_tenant_channel"][
        "referred_columns"
    ] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert override_fks["fk_revenue_manual_overrides_tenant_channel"][
        "constrained_columns"
    ] == [
        "tenant_id",
        "youtube_channel_id",
    ]


def test_downgrade_restores_global_youtube_channel_identity():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_pre_migration_schema(connection)
        migration = _load_migration()

        _execute_migration(connection, migration, "upgrade")
        _execute_migration(connection, migration, "downgrade")

        inspector = inspect(connection)
        youtube_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("youtube_channels")
        }
        monthly_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("monthly_channel_revenue_facts")
        }

    assert youtube_uniques["uq_youtube_channels_youtube_channel_id"] == [
        "youtube_channel_id"
    ]
    assert monthly_fks["fk_monthly_channel_revenue_facts_youtube_channel_id"][
        "constrained_columns"
    ] == ["youtube_channel_id"]
    assert monthly_fks["fk_monthly_channel_revenue_facts_youtube_channel_id"][
        "referred_columns"
    ] == ["youtube_channel_id"]


def _setup_pre_migration_schema(connection: Connection) -> None:
    metadata = MetaData()
    Table(
        "youtube_channels",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("tenant_id", Text(), nullable=False),
        Column("youtube_channel_id", Text(), nullable=False),
        UniqueConstraint(
            "youtube_channel_id",
            name="uq_youtube_channels_youtube_channel_id",
        ),
    )
    Table(
        "monthly_channel_revenue_facts",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("tenant_id", Text(), nullable=False),
        Column("youtube_channel_id", Text(), nullable=False),
        Column("month", Text(), nullable=False),
        Column("source_kind", Text(), nullable=False),
        ForeignKeyConstraint(
            ["youtube_channel_id"],
            ["youtube_channels.youtube_channel_id"],
            name="fk_monthly_channel_revenue_facts_youtube_channel_id",
            ondelete="RESTRICT",
        ),
    )
    Table(
        "revenue_manual_overrides",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("tenant_id", Text(), nullable=False),
        Column("youtube_channel_id", Text(), nullable=False),
        ForeignKeyConstraint(
            ["youtube_channel_id"],
            ["youtube_channels.youtube_channel_id"],
            name="fk_revenue_manual_overrides_youtube_channel_id",
            ondelete="RESTRICT",
        ),
    )
    metadata.create_all(connection)


def _build_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute_migration(
    connection: Connection,
    migration: ModuleType,
    action: str,
) -> None:
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = migration.op
    try:
        migration.op = operations
        getattr(migration, action)()
    finally:
        migration.op = original_op
