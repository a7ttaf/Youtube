"""Migration round-trip tests for ``20260517_0001_tenant_id_on_operational_tables``.

The migration adds a ``tenant_id`` column (NOT NULL, default UMS, FK to
tenants, indexed) to every tenant-scoped operational table.

Rather than chain the 13 ancestor migrations (some of which use PG-only
features like ``CREATE EXTENSION`` and ``JSONB`` that SQLite cannot
render), we materialise a *minimal* pre-S2.4a schema in-line: the
``tenants`` table with its one row, plus an empty stub for each of the
18 operational tables. The migration only does ``ALTER TABLE ADD COLUMN``
+ ``ADD CONSTRAINT`` + ``CREATE INDEX``, so the minimal stub is enough
to exercise every code path. End-to-end migration integrity against
the full chain stays the responsibility of CI's Postgres job once the
mirror workflow re-enables.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.engine import Connection, Engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions"
TARGET_MIGRATION = "20260517_0001_tenant_id_on_operational_tables"
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# Source-of-truth list of tables the target migration should touch.
# Duplicated from the migration module so accidental drift between the
# two raises an assertion at test time rather than silently corrupting
# a real schema.
EXPECTED_TABLES: tuple[str, ...] = (
    "users",
    "access_scopes",
    "user_role_assignments",
    "user_permission_grants",
    "audit_logs",
    "api_connector_credentials",
    "org_units",
    "youtube_channels",
    "channel_groups",
    "channel_group_members",
    "finance_month_close",
    "monthly_channel_revenue_facts",
    "revenue_manual_overrides",
    "adsense_payments",
    "bank_reconciliation_entries",
    "raw_report_files",
    "number_explanations",
    "export_jobs",
)


def test_target_migration_module_lists_same_tables_as_test_fixture():
    """Guard against drift between the migration's table list and ours."""
    target = _load_migration(TARGET_MIGRATION)

    assert tuple(target.TENANT_SCOPED_TABLES) == EXPECTED_TABLES


def test_migration_adds_tenant_id_to_every_operational_table():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        inspector = inspect(connection)
        for table in EXPECTED_TABLES:
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            assert "tenant_id" in columns, f"{table} missing tenant_id column"
            assert columns["tenant_id"]["nullable"] is False, (
                f"{table}.tenant_id should be NOT NULL"
            )

            index_names = {i["name"] for i in inspector.get_indexes(table)}
            assert f"ix_{table}_tenant_id" in index_names, (
                f"{table} missing ix_{table}_tenant_id index"
            )

            tenant_fk = next(
                (
                    fk
                    for fk in inspector.get_foreign_keys(table)
                    if fk.get("name") == f"fk_{table}_tenant_id"
                ),
                None,
            )
            assert tenant_fk is not None, (
                f"{table} missing fk_{table}_tenant_id foreign key"
            )
            assert tenant_fk.get("constrained_columns") == ["tenant_id"], (
                f"{table} FK constrained columns mismatch"
            )
            assert tenant_fk.get("referred_table") == "tenants", (
                f"{table}.tenant_id FK should reference tenants"
            )
            assert tenant_fk.get("referred_columns") == ["id"], (
                f"{table} FK referred columns mismatch"
            )
            assert (tenant_fk.get("options") or {}).get("ondelete") == "RESTRICT", (
                f"{table} FK should be ON DELETE RESTRICT"
            )


def test_existing_rows_get_ums_tenant_id_via_default():
    """A row inserted *before* the migration should backfill to UMS."""
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        # Pre-existing row with no tenant_id.
        connection.execute(text("INSERT INTO users (id) VALUES ('pre')"))

        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        row = connection.execute(
            text("SELECT tenant_id FROM users WHERE id = 'pre'")
        ).one()

    assert _strip_uuid(str(row.tenant_id)) == _strip_uuid(UMS_TENANT_ID)


def test_new_inserts_without_tenant_id_get_ums_default():
    """After the migration, an INSERT that omits tenant_id should default."""
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        connection.execute(text("INSERT INTO users (id) VALUES ('post')"))
        row = connection.execute(
            text("SELECT tenant_id FROM users WHERE id = 'post'")
        ).one()

    assert _strip_uuid(str(row.tenant_id)) == _strip_uuid(UMS_TENANT_ID)


def test_tenant_scoped_constraints_are_rewritten():
    """Tenant-scoped uniqueness, keys, and FKs include tenant_id."""
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        inspector = inspect(connection)

        access_scope_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("access_scopes")
        }
        assert access_scope_indexes["uq_access_scopes_scope_type_scope_id"] == [
            "tenant_id",
            "scope_type",
            "scope_id",
        ]
        assert access_scope_indexes["uq_access_scopes_global_singleton"] == [
            "tenant_id",
            "scope_type",
        ]
        access_scope_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("access_scopes")
        }
        assert access_scope_uniques["uq_access_scopes_tenant_id_id"] == [
            "tenant_id",
            "id",
        ]

        role_assignment_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("user_role_assignments")
        }
        assert role_assignment_indexes["uq_active_user_role_scope"] == [
            "tenant_id",
            "user_id",
            "role_key",
            "scope_id",
        ]
        role_assignment_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_role_assignments")
        }
        role_scope_fk = role_assignment_fks["fk_user_role_assignments_tenant_scope"]
        assert role_scope_fk["constrained_columns"] == ["tenant_id", "scope_id"]
        assert role_scope_fk["referred_table"] == "access_scopes"
        assert role_scope_fk["referred_columns"] == ["tenant_id", "id"]
        assert (role_scope_fk.get("options") or {}).get("ondelete") == "RESTRICT"

        permission_grant_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("user_permission_grants")
        }
        assert permission_grant_indexes["uq_active_user_permission_scope"] == [
            "tenant_id",
            "user_id",
            "permission_key",
            "scope_id",
        ]
        permission_grant_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_permission_grants")
        }
        permission_scope_fk = permission_grant_fks[
            "fk_user_permission_grants_tenant_scope"
        ]
        assert permission_scope_fk["constrained_columns"] == [
            "tenant_id",
            "scope_id",
        ]
        assert permission_scope_fk["referred_table"] == "access_scopes"
        assert permission_scope_fk["referred_columns"] == ["tenant_id", "id"]
        assert (permission_scope_fk.get("options") or {}).get("ondelete") == "RESTRICT"

        user_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("users")
        }
        assert user_uniques["uq_users_tenant_id_id"] == ["tenant_id", "id"]

        user_email_index_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'uq_users_email_lower'"
            )
        ).scalar_one()
        assert "tenant_id" in user_email_index_sql
        assert "lower(email)" in user_email_index_sql

        api_connector_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("api_connector_credentials")
        }
        assert api_connector_indexes[
            "uq_api_connector_credentials_connector_account"
        ] == [
            "tenant_id",
            "connector_key",
            "account_id",
        ]

        monthly_revenue_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints(
                "monthly_channel_revenue_facts"
            )
        }
        assert monthly_revenue_uniques["uq_monthly_channel_revenue_source"] == [
            "tenant_id",
            "month",
            "youtube_channel_id",
            "source_kind",
        ]

        bank_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints(
                "bank_reconciliation_entries"
            )
        }
        assert bank_uniques["uq_bank_reconciliation_month_reference"] == [
            "tenant_id",
            "month",
            "bank_reference",
        ]

        raw_report_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("raw_report_files")
        }
        assert raw_report_uniques[
            "uq_raw_report_files_source_type_month_checksum"
        ] == [
            "tenant_id",
            "source",
            "report_type",
            "report_month",
            "checksum",
        ]

        explanation_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("number_explanations")
        }
        assert explanation_uniques["uq_number_explanations_entity_metric_month"] == [
            "tenant_id",
            "month",
            "entity_type",
            "entity_id",
            "metric",
        ]

        finance_pk = inspector.get_pk_constraint("finance_month_close")
        assert finance_pk["constrained_columns"] == ["tenant_id", "month"]

        adsense_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("adsense_payments")
        }
        assert adsense_uniques["uq_adsense_payments_month_name"] == [
            "tenant_id",
            "month",
            "payment_name",
        ]

        yt_channel_fks = {
            fk["name"]: fk for fk in inspector.get_foreign_keys("youtube_channels")
        }
        yt_org_fk = yt_channel_fks["fk_youtube_channels_tenant_org_unit"]
        assert yt_org_fk["constrained_columns"] == [
            "tenant_id",
            "primary_org_unit_id",
        ]
        assert yt_org_fk["referred_table"] == "org_units"
        assert yt_org_fk["referred_columns"] == ["tenant_id", "id"]
        assert (yt_org_fk.get("options") or {}).get("ondelete") == "RESTRICT"

        youtube_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("youtube_channels")
        }
        channel_group_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("channel_groups")
        }
        assert youtube_uniques["uq_youtube_channels_tenant_id_id"] == [
            "tenant_id",
            "id",
        ]
        assert channel_group_uniques["uq_channel_groups_tenant_id_id"] == [
            "tenant_id",
            "id",
        ]

        group_member_fks = {
            fk["name"]: fk for fk in inspector.get_foreign_keys("channel_group_members")
        }
        tenant_group_fk = group_member_fks["fk_channel_group_members_tenant_group"]
        assert tenant_group_fk["constrained_columns"] == ["tenant_id", "group_id"]
        assert tenant_group_fk["referred_table"] == "channel_groups"
        assert tenant_group_fk["referred_columns"] == ["tenant_id", "id"]
        assert (tenant_group_fk.get("options") or {}).get("ondelete") == "CASCADE"

        tenant_channel_fk = group_member_fks["fk_channel_group_members_tenant_channel"]
        assert tenant_channel_fk["constrained_columns"] == [
            "tenant_id",
            "channel_id",
        ]
        assert tenant_channel_fk["referred_table"] == "youtube_channels"
        assert tenant_channel_fk["referred_columns"] == ["tenant_id", "id"]
        assert (tenant_channel_fk.get("options") or {}).get("ondelete") == "CASCADE"

        org_units_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("org_units")
        }
        assert org_units_uniques["uq_org_units_tenant_id_id"] == ["tenant_id", "id"]

        org_units_fks = {
            fk["name"]: fk for fk in inspector.get_foreign_keys("org_units")
        }
        parent_fk = org_units_fks["fk_org_units_tenant_parent"]
        assert parent_fk["constrained_columns"] == ["tenant_id", "parent_id"]
        assert parent_fk["referred_table"] == "org_units"
        assert parent_fk["referred_columns"] == ["tenant_id", "id"]
        assert (parent_fk.get("options") or {}).get("ondelete") == "RESTRICT"

        role_assign_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_role_assignments")
        }
        ura_user_fk = role_assign_fks["fk_user_role_assignments_tenant_user"]
        assert ura_user_fk["constrained_columns"] == ["tenant_id", "user_id"]
        assert ura_user_fk["referred_table"] == "users"
        assert ura_user_fk["referred_columns"] == ["tenant_id", "id"]
        assert (ura_user_fk.get("options") or {}).get("ondelete") == "CASCADE"
        ura_ab_fk = role_assign_fks["fk_user_role_assignments_tenant_assigned_by"]
        assert ura_ab_fk["constrained_columns"] == ["tenant_id", "assigned_by"]
        assert ura_ab_fk["referred_table"] == "users"
        assert ura_ab_fk["referred_columns"] == ["tenant_id", "id"]
        ura_rb_fk = role_assign_fks["fk_user_role_assignments_tenant_revoked_by"]
        assert ura_rb_fk["constrained_columns"] == ["tenant_id", "revoked_by"]
        assert ura_rb_fk["referred_table"] == "users"
        assert ura_rb_fk["referred_columns"] == ["tenant_id", "id"]

        perm_grant_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_permission_grants")
        }
        upg_user_fk = perm_grant_fks["fk_user_permission_grants_tenant_user"]
        assert upg_user_fk["constrained_columns"] == ["tenant_id", "user_id"]
        assert upg_user_fk["referred_table"] == "users"
        assert upg_user_fk["referred_columns"] == ["tenant_id", "id"]
        assert (upg_user_fk.get("options") or {}).get("ondelete") == "CASCADE"
        upg_gb_fk = perm_grant_fks["fk_user_permission_grants_tenant_granted_by"]
        assert upg_gb_fk["constrained_columns"] == ["tenant_id", "granted_by"]
        assert upg_gb_fk["referred_table"] == "users"
        assert upg_gb_fk["referred_columns"] == ["tenant_id", "id"]
        upg_rb_fk = perm_grant_fks["fk_user_permission_grants_tenant_revoked_by"]
        assert upg_rb_fk["constrained_columns"] == ["tenant_id", "revoked_by"]
        assert upg_rb_fk["referred_table"] == "users"
        assert upg_rb_fk["referred_columns"] == ["tenant_id", "id"]

        audit_fks = {
            fk["name"]: fk for fk in inspector.get_foreign_keys("audit_logs")
        }
        al_user_fk = audit_fks["fk_audit_logs_tenant_user"]
        assert al_user_fk["constrained_columns"] == ["tenant_id", "user_id"]
        assert al_user_fk["referred_table"] == "users"
        assert al_user_fk["referred_columns"] == ["tenant_id", "id"]

        connector_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("api_connector_credentials")
        }
        acc_cb_fk = connector_fks["fk_api_connector_credentials_tenant_created_by"]
        assert acc_cb_fk["constrained_columns"] == ["tenant_id", "created_by"]
        assert acc_cb_fk["referred_table"] == "users"
        assert acc_cb_fk["referred_columns"] == ["tenant_id", "id"]
        acc_ub_fk = connector_fks["fk_api_connector_credentials_tenant_updated_by"]
        assert acc_ub_fk["constrained_columns"] == ["tenant_id", "updated_by"]
        assert acc_ub_fk["referred_table"] == "users"
        assert acc_ub_fk["referred_columns"] == ["tenant_id", "id"]


def test_downgrade_removes_tenant_id_from_every_table():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        target = _load_migration(TARGET_MIGRATION)

        _execute_migration(connection, target, "upgrade")
        _execute_migration(connection, target, "downgrade")

        inspector = inspect(connection)
        for table in EXPECTED_TABLES:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "tenant_id" not in columns, (
                f"{table} still has tenant_id after downgrade"
            )

        # finance_month_close PK must be restored to (month,) only.
        fmc_pk = inspector.get_pk_constraint("finance_month_close")
        assert fmc_pk["constrained_columns"] == ["month"]

        # access_scopes partial indexes must not include tenant_id after downgrade.
        access_scope_idx = {
            idx["name"]: idx["column_names"]
            for idx in inspector.get_indexes("access_scopes")
        }
        assert "tenant_id" not in access_scope_idx.get(
            "uq_access_scopes_scope_type_scope_id", []
        )
        assert "tenant_id" not in access_scope_idx.get(
            "uq_access_scopes_global_singleton", []
        )

        # Nullable actor/audit FKs must be restored with SET NULL (original semantics).
        # ON DELETE SET NULL is impossible on composite FKs that include a NOT NULL
        # tenant_id, so downgrade reverts to single-column FKs with SET NULL.
        def _ondelete(fk_dict: dict) -> str | None:
            return (fk_dict.get("options") or {}).get("ondelete")

        ura_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_role_assignments")
        }
        assert _ondelete(ura_fks["user_role_assignments_assigned_by_fkey"]) == (
            "SET NULL"
        )
        assert _ondelete(ura_fks["user_role_assignments_revoked_by_fkey"]) == (
            "SET NULL"
        )

        upg_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("user_permission_grants")
        }
        assert _ondelete(upg_fks["user_permission_grants_granted_by_fkey"]) == (
            "SET NULL"
        )
        assert _ondelete(upg_fks["user_permission_grants_revoked_by_fkey"]) == (
            "SET NULL"
        )

        audit_fks = {
            fk["name"]: fk for fk in inspector.get_foreign_keys("audit_logs")
        }
        assert _ondelete(audit_fks["audit_logs_user_id_fkey"]) == "SET NULL"

        acc_fks = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("api_connector_credentials")
        }
        assert _ondelete(acc_fks["api_connector_credentials_created_by_fkey"]) == (
            "SET NULL"
        )
        assert _ondelete(acc_fks["api_connector_credentials_updated_by_fkey"]) == (
            "SET NULL"
        )


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _setup_minimal_pre_state(connection: Connection) -> None:
    """Create the bare minimum schema the target migration needs.

    Just enough so ``ALTER TABLE … ADD COLUMN tenant_id`` plus the FK
    to ``tenants(id)`` will succeed:

    * ``tenants`` table with the UMS row at the deterministic UUID.
    * Operational tables with just enough pre-existing keys/indexes/FKs
      to verify the tenant-aware rewrites.
    """
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Text(), primary_key=True))
    users = Table(
        "users",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("email", Text(), nullable=True),
    )
    Index("uq_users_email_lower", func.lower(users.c.email), unique=True)
    access_scopes = Table(
        "access_scopes",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("scope_type", Text(), nullable=False),
        Column("scope_id", Text(), nullable=True),
    )
    Index(
        "uq_access_scopes_scope_type_scope_id",
        access_scopes.c.scope_type,
        access_scopes.c.scope_id,
        unique=True,
        sqlite_where=text("scope_id IS NOT NULL"),
    )
    Index(
        "uq_access_scopes_global_singleton",
        access_scopes.c.scope_type,
        unique=True,
        sqlite_where=text("scope_type = 'global' AND scope_id IS NULL"),
    )
    user_role_assignments = Table(
        "user_role_assignments",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("user_id", Text(), nullable=False),
        Column("role_key", Text(), nullable=False),
        Column("scope_id", Text(), nullable=False),
        Column("assigned_by", Text(), nullable=True),
        Column("revoked_by", Text(), nullable=True),
        Column("active", Boolean(), nullable=False),
        ForeignKeyConstraint(
            ["scope_id"],
            ["access_scopes.id"],
            name="user_role_assignments_scope_id_fkey",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="user_role_assignments_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name="user_role_assignments_assigned_by_fkey",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name="user_role_assignments_revoked_by_fkey",
            ondelete="SET NULL",
        ),
    )
    Index(
        "uq_active_user_role_scope",
        user_role_assignments.c.user_id,
        user_role_assignments.c.role_key,
        user_role_assignments.c.scope_id,
        unique=True,
        sqlite_where=text("active = true"),
    )
    user_permission_grants = Table(
        "user_permission_grants",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("user_id", Text(), nullable=False),
        Column("permission_key", Text(), nullable=False),
        Column("scope_id", Text(), nullable=False),
        Column("granted_by", Text(), nullable=True),
        Column("revoked_by", Text(), nullable=True),
        Column("active", Boolean(), nullable=False),
        ForeignKeyConstraint(
            ["scope_id"],
            ["access_scopes.id"],
            name="user_permission_grants_scope_id_fkey",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="user_permission_grants_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name="user_permission_grants_granted_by_fkey",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name="user_permission_grants_revoked_by_fkey",
            ondelete="SET NULL",
        ),
    )
    Index(
        "uq_active_user_permission_scope",
        user_permission_grants.c.user_id,
        user_permission_grants.c.permission_key,
        user_permission_grants.c.scope_id,
        unique=True,
        sqlite_where=text("active = true"),
    )
    api_connector_credentials = Table(
        "api_connector_credentials",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("connector_key", Text(), nullable=False),
        Column("account_id", Text(), nullable=False),
        Column("created_by", Text(), nullable=True),
        Column("updated_by", Text(), nullable=True),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="api_connector_credentials_created_by_fkey",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="api_connector_credentials_updated_by_fkey",
            ondelete="SET NULL",
        ),
    )
    Index(
        "uq_api_connector_credentials_connector_account",
        api_connector_credentials.c.connector_key,
        api_connector_credentials.c.account_id,
        unique=True,
    )
    org_units = Table(
        "org_units",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("parent_id", Text(), nullable=True),
        ForeignKeyConstraint(
            ["parent_id"],
            ["org_units.id"],
            name="org_units_parent_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    del org_units  # referenced only to register with metadata
    Table(
        "youtube_channels",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("primary_org_unit_id", Text(), nullable=True),
        ForeignKeyConstraint(
            ["primary_org_unit_id"],
            ["org_units.id"],
            name="youtube_channels_primary_org_unit_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    Table("channel_groups", metadata, Column("id", Text(), primary_key=True))
    Table(
        "channel_group_members",
        metadata,
        Column("group_id", Text(), nullable=False),
        Column("channel_id", Text(), nullable=False),
        PrimaryKeyConstraint(
            "group_id",
            "channel_id",
            name="channel_group_members_pkey",
        ),
        ForeignKeyConstraint(
            ["group_id"],
            ["channel_groups.id"],
            name="channel_group_members_group_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id"],
            ["youtube_channels.id"],
            name="channel_group_members_channel_id_fkey",
            ondelete="CASCADE",
        ),
    )
    Table(
        "finance_month_close",
        metadata,
        Column("month", Text(), nullable=False),
        PrimaryKeyConstraint("month", name="finance_month_close_pkey"),
    )
    Table(
        "monthly_channel_revenue_facts",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("month", Text(), nullable=False),
        Column("youtube_channel_id", Text(), nullable=False),
        Column("source_kind", Text(), nullable=False),
        UniqueConstraint(
            "month",
            "youtube_channel_id",
            "source_kind",
            name="uq_monthly_channel_revenue_source",
        ),
    )
    Table(
        "bank_reconciliation_entries",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("month", Text(), nullable=False),
        Column("bank_reference", Text(), nullable=False),
        UniqueConstraint(
            "month",
            "bank_reference",
            name="uq_bank_reconciliation_month_reference",
        ),
    )
    Table(
        "raw_report_files",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("source", Text(), nullable=False),
        Column("report_type", Text(), nullable=False),
        Column("report_month", Text(), nullable=False),
        Column("checksum", Text(), nullable=False),
        UniqueConstraint(
            "source",
            "report_type",
            "report_month",
            "checksum",
            name="uq_raw_report_files_source_type_month_checksum",
        ),
    )
    Table(
        "adsense_payments",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("month", Text(), nullable=False),
        Column("payment_name", Text(), nullable=False),
        UniqueConstraint(
            "month",
            "payment_name",
            name="uq_adsense_payments_month_name",
        ),
    )
    Table(
        "number_explanations",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("month", Text(), nullable=False),
        Column("entity_type", Text(), nullable=False),
        Column("entity_id", Text(), nullable=False),
        Column("metric", Text(), nullable=False),
        UniqueConstraint(
            "month",
            "entity_type",
            "entity_id",
            "metric",
            name="uq_number_explanations_entity_metric_month",
        ),
    )
    Table(
        "audit_logs",
        metadata,
        Column("id", Text(), primary_key=True),
        Column("user_id", Text(), nullable=True),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="audit_logs_user_id_fkey",
            ondelete="SET NULL",
        ),
    )
    special_tables = {
        "access_scopes",
        "api_connector_credentials",
        "adsense_payments",
        "audit_logs",
        "bank_reconciliation_entries",
        "channel_group_members",
        "channel_groups",
        "finance_month_close",
        "monthly_channel_revenue_facts",
        "number_explanations",
        "org_units",
        "raw_report_files",
        "tenants",
        "user_permission_grants",
        "user_role_assignments",
        "users",
        "youtube_channels",
    }
    for table in EXPECTED_TABLES:
        if table not in special_tables:
            Table(table, metadata, Column("id", Text()))

    metadata.create_all(connection)
    connection.execute(
        text("INSERT INTO tenants (id) VALUES (:tenant_id)"),
        {"tenant_id": UMS_TENANT_ID},
    )


def _build_engine() -> Engine:
    # FK enforcement (PRAGMA foreign_keys = ON) is intentionally NOT enabled here.
    # Alembic's batch_alter_table rewrites tables by creating a temp table, copying
    # data, then renaming — a pattern that causes SQLite to raise "foreign key
    # mismatch" on self-referential tables (e.g., org_units → org_units) when FK
    # enforcement is active. ON DELETE behaviour and constraint structure are still
    # exercised through inspector metadata reflection (get_foreign_keys returns the
    # ON DELETE action regardless of the PRAGMA setting). Full FK enforcement runs
    # against PostgreSQL in CI.
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    return engine


def _load_migration(name: str) -> ModuleType:
    path = VERSIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
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


def _strip_uuid(value: str) -> str:
    return value.replace("-", "")
