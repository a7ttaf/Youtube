"""Add tenant_id to operational tables.

Revision ID: 20260517_0001
Revises: 20260516_0001
Create Date: 2026-05-17

S2.4a — first slice of the breaking-change rollout for multi-tenancy.

Every tenant-scoped operational table gains:

* ``tenant_id`` (UUID, NOT NULL)
* ``server_default = UMS_TENANT_ID`` — so existing rows backfill to
  the seeded UMS tenant and so pre-existing test fixtures + code paths
  that don't yet pass a tenant_id continue to write valid rows. A
  follow-up slice will drop the default once every consumer sets the
  value explicitly.
* FK to ``tenants(id)`` ON DELETE RESTRICT — deleting a tenant is a
  deliberate platform-admin action; the DB refuses the cascade.
* Index ``ix_<table>_tenant_id`` on ``tenant_id`` alone — sufficient
  to support the per-tenant filtering that Row-Level Security policies
  will add in S2.4b. Composite indexes for hot read paths land in
  later slices once we see real query plans.

What does *not* get ``tenant_id`` and why:

* ``roles`` / ``permissions`` / ``role_permission_assignments`` —
  definition catalogs shared across tenants.
* ``tenants`` / ``platform_admins`` — sit above the tenant model.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260517_0001"
down_revision = "20260516_0001"
branch_labels = None
depends_on = None


# Keep this historical migration self-contained and immutable. This matches
# the UMS tenant seed value from 20260516_0001_tenants_foundation.py.
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# The canonical list of tenant-scoped operational tables. Order matters
# only for downgrade (we drop indexes/FKs in reverse) — upgrade is
# additive and order-independent.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    # auth / security
    "users",
    "access_scopes",
    "user_role_assignments",
    "user_permission_grants",
    "audit_logs",
    "api_connector_credentials",
    # org
    "org_units",
    "youtube_channels",
    "channel_groups",
    "channel_group_members",
    # finance
    "finance_month_close",
    "monthly_channel_revenue_facts",
    "revenue_manual_overrides",
    "adsense_payments",
    "bank_reconciliation_entries",
    # reports / explanation
    "raw_report_files",
    "number_explanations",
    "export_jobs",
)


def upgrade() -> None:
    for table_name in TENANT_SCOPED_TABLES:
        _add_tenant_id_to(table_name)
    _scope_access_scope_unique_indexes()
    _scope_access_assignment_constraints()
    _scope_tenant_unique_constraints()
    _scope_finance_month_close_primary_key()
    _scope_channel_group_membership_constraints()
    _scope_adsense_payment_unique_constraint()
    _scope_org_units_parent_fk()


def downgrade() -> None:
    _unscope_org_units_parent_fk()
    _unscope_adsense_payment_unique_constraint()
    _unscope_channel_group_membership_constraints()
    _unscope_finance_month_close_primary_key()
    _unscope_tenant_unique_constraints()
    _unscope_access_assignment_constraints()
    _unscope_access_scope_unique_indexes()
    for table_name in reversed(TENANT_SCOPED_TABLES):
        _drop_tenant_id_from(table_name)


def _add_tenant_id_to(table_name: str) -> None:
    """Add the tenant_id column, FK, and index to ``table_name``.

    Uses ``batch_alter_table`` so the operation also works on SQLite,
    which cannot ``ALTER TABLE ADD CONSTRAINT FOREIGN KEY`` directly.
    On PostgreSQL ``batch_alter_table`` collapses to the underlying
    ``ALTER TABLE`` statements without rewriting the table.
    """
    fk_name = f"fk_{table_name}_tenant_id"
    index_name = f"ix_{table_name}_tenant_id"
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                nullable=False,
                server_default=sa.text(f"'{UMS_TENANT_ID}'"),
            )
        )
        batch_op.create_foreign_key(
            fk_name,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(index_name, table_name, ["tenant_id"])


def _drop_tenant_id_from(table_name: str) -> None:
    fk_name = f"fk_{table_name}_tenant_id"
    index_name = f"ix_{table_name}_tenant_id"
    op.drop_index(index_name, table_name=table_name)
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(fk_name, type_="foreignkey")
        batch_op.drop_column("tenant_id")


def _drop_index(index_name: str, *, table_name: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text(f'DROP INDEX IF EXISTS "{index_name}"'))
        return
    op.drop_index(index_name, table_name=table_name)


def _scope_access_scope_unique_indexes() -> None:
    op.drop_index("uq_access_scopes_global_singleton", table_name="access_scopes")
    op.drop_index("uq_access_scopes_scope_type_scope_id", table_name="access_scopes")
    op.create_index(
        "uq_access_scopes_scope_type_scope_id",
        "access_scopes",
        ["tenant_id", "scope_type", "scope_id"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NOT NULL"),
        sqlite_where=sa.text("scope_id IS NOT NULL"),
    )
    op.create_index(
        "uq_access_scopes_global_singleton",
        "access_scopes",
        ["tenant_id", "scope_type"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'global' AND scope_id IS NULL"),
        sqlite_where=sa.text("scope_type = 'global' AND scope_id IS NULL"),
    )


def _unscope_access_scope_unique_indexes() -> None:
    op.drop_index("uq_access_scopes_global_singleton", table_name="access_scopes")
    op.drop_index("uq_access_scopes_scope_type_scope_id", table_name="access_scopes")
    op.create_index(
        "uq_access_scopes_scope_type_scope_id",
        "access_scopes",
        ["scope_type", "scope_id"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NOT NULL"),
        sqlite_where=sa.text("scope_id IS NOT NULL"),
    )
    op.create_index(
        "uq_access_scopes_global_singleton",
        "access_scopes",
        ["scope_type"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'global' AND scope_id IS NULL"),
        sqlite_where=sa.text("scope_type = 'global' AND scope_id IS NULL"),
    )


def _scope_access_assignment_constraints() -> None:
    with op.batch_alter_table("access_scopes") as batch_op:
        batch_op.create_unique_constraint(
            "uq_access_scopes_tenant_id_id",
            ["tenant_id", "id"],
        )
    with op.batch_alter_table("user_role_assignments") as batch_op:
        batch_op.drop_constraint(
            "user_role_assignments_scope_id_fkey",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_user_role_assignments_tenant_scope",
            "access_scopes",
            ["tenant_id", "scope_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
    op.drop_index("uq_active_user_role_scope", table_name="user_role_assignments")
    op.create_index(
        "uq_active_user_role_scope",
        "user_role_assignments",
        ["tenant_id", "user_id", "role_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = true"),
    )

    with op.batch_alter_table("user_permission_grants") as batch_op:
        batch_op.drop_constraint(
            "user_permission_grants_scope_id_fkey",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_user_permission_grants_tenant_scope",
            "access_scopes",
            ["tenant_id", "scope_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
    op.drop_index(
        "uq_active_user_permission_scope",
        table_name="user_permission_grants",
    )
    op.create_index(
        "uq_active_user_permission_scope",
        "user_permission_grants",
        ["tenant_id", "user_id", "permission_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = true"),
    )


def _unscope_access_assignment_constraints() -> None:
    op.drop_index(
        "uq_active_user_permission_scope",
        table_name="user_permission_grants",
    )
    op.create_index(
        "uq_active_user_permission_scope",
        "user_permission_grants",
        ["user_id", "permission_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = true"),
    )
    with op.batch_alter_table("user_permission_grants") as batch_op:
        batch_op.drop_constraint(
            "fk_user_permission_grants_tenant_scope",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "user_permission_grants_scope_id_fkey",
            "access_scopes",
            ["scope_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.drop_index("uq_active_user_role_scope", table_name="user_role_assignments")
    op.create_index(
        "uq_active_user_role_scope",
        "user_role_assignments",
        ["user_id", "role_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = true"),
    )
    with op.batch_alter_table("user_role_assignments") as batch_op:
        batch_op.drop_constraint(
            "fk_user_role_assignments_tenant_scope",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "user_role_assignments_scope_id_fkey",
            "access_scopes",
            ["scope_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("access_scopes") as batch_op:
        batch_op.drop_constraint("uq_access_scopes_tenant_id_id", type_="unique")


def _scope_tenant_unique_constraints() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.create_unique_constraint("uq_users_tenant_id_id", ["tenant_id", "id"])

    _drop_index("uq_users_email_lower", table_name="users")
    op.create_index(
        "uq_users_email_lower",
        "users",
        ["tenant_id", sa.text("lower(email)")],
        unique=True,
    )

    op.drop_index(
        "uq_api_connector_credentials_connector_account",
        table_name="api_connector_credentials",
    )
    op.create_index(
        "uq_api_connector_credentials_connector_account",
        "api_connector_credentials",
        ["tenant_id", "connector_key", "account_id"],
        unique=True,
    )

    with op.batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.drop_constraint(
            "uq_monthly_channel_revenue_source",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_monthly_channel_revenue_source",
            ["tenant_id", "month", "youtube_channel_id", "source_kind"],
        )

    with op.batch_alter_table("bank_reconciliation_entries") as batch_op:
        batch_op.drop_constraint(
            "uq_bank_reconciliation_month_reference",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_bank_reconciliation_month_reference",
            ["tenant_id", "month", "bank_reference"],
        )

    with op.batch_alter_table("raw_report_files") as batch_op:
        batch_op.drop_constraint(
            "uq_raw_report_files_source_type_month_checksum",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_raw_report_files_source_type_month_checksum",
            ["tenant_id", "source", "report_type", "report_month", "checksum"],
        )

    with op.batch_alter_table("number_explanations") as batch_op:
        batch_op.drop_constraint(
            "uq_number_explanations_entity_metric_month",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_number_explanations_entity_metric_month",
            ["tenant_id", "month", "entity_type", "entity_id", "metric"],
        )


def _unscope_tenant_unique_constraints() -> None:
    with op.batch_alter_table("number_explanations") as batch_op:
        batch_op.drop_constraint(
            "uq_number_explanations_entity_metric_month",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_number_explanations_entity_metric_month",
            ["month", "entity_type", "entity_id", "metric"],
        )

    with op.batch_alter_table("raw_report_files") as batch_op:
        batch_op.drop_constraint(
            "uq_raw_report_files_source_type_month_checksum",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_raw_report_files_source_type_month_checksum",
            ["source", "report_type", "report_month", "checksum"],
        )

    with op.batch_alter_table("bank_reconciliation_entries") as batch_op:
        batch_op.drop_constraint(
            "uq_bank_reconciliation_month_reference",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_bank_reconciliation_month_reference",
            ["month", "bank_reference"],
        )

    with op.batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.drop_constraint(
            "uq_monthly_channel_revenue_source",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_monthly_channel_revenue_source",
            ["month", "youtube_channel_id", "source_kind"],
        )

    op.drop_index(
        "uq_api_connector_credentials_connector_account",
        table_name="api_connector_credentials",
    )
    op.create_index(
        "uq_api_connector_credentials_connector_account",
        "api_connector_credentials",
        ["connector_key", "account_id"],
        unique=True,
    )

    _drop_index("uq_users_email_lower", table_name="users")
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_tenant_id_id", type_="unique")


def _scope_finance_month_close_primary_key() -> None:
    with op.batch_alter_table("finance_month_close") as batch_op:
        batch_op.drop_constraint("finance_month_close_pkey", type_="primary")
        batch_op.create_primary_key(
            "pk_finance_month_close",
            ["tenant_id", "month"],
        )


def _unscope_finance_month_close_primary_key() -> None:
    with op.batch_alter_table("finance_month_close") as batch_op:
        batch_op.drop_constraint("pk_finance_month_close", type_="primary")
        batch_op.create_primary_key("finance_month_close_pkey", ["month"])


def _scope_channel_group_membership_constraints() -> None:
    with op.batch_alter_table("youtube_channels") as batch_op:
        batch_op.create_unique_constraint(
            "uq_youtube_channels_tenant_id_id",
            ["tenant_id", "id"],
        )
    with op.batch_alter_table("channel_groups") as batch_op:
        batch_op.create_unique_constraint(
            "uq_channel_groups_tenant_id_id",
            ["tenant_id", "id"],
        )
    with op.batch_alter_table("channel_group_members") as batch_op:
        batch_op.drop_constraint(
            "channel_group_members_group_id_fkey",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "channel_group_members_channel_id_fkey",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_channel_group_members_tenant_group",
            "channel_groups",
            ["tenant_id", "group_id"],
            ["tenant_id", "id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_channel_group_members_tenant_channel",
            "youtube_channels",
            ["tenant_id", "channel_id"],
            ["tenant_id", "id"],
            ondelete="CASCADE",
        )


def _unscope_channel_group_membership_constraints() -> None:
    with op.batch_alter_table("channel_group_members") as batch_op:
        batch_op.drop_constraint(
            "fk_channel_group_members_tenant_channel",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_channel_group_members_tenant_group",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "channel_group_members_group_id_fkey",
            "channel_groups",
            ["group_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "channel_group_members_channel_id_fkey",
            "youtube_channels",
            ["channel_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("channel_groups") as batch_op:
        batch_op.drop_constraint("uq_channel_groups_tenant_id_id", type_="unique")
    with op.batch_alter_table("youtube_channels") as batch_op:
        batch_op.drop_constraint("uq_youtube_channels_tenant_id_id", type_="unique")


def _scope_adsense_payment_unique_constraint() -> None:
    with op.batch_alter_table("adsense_payments") as batch_op:
        batch_op.drop_constraint("uq_adsense_payments_month_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_adsense_payments_month_name",
            ["tenant_id", "month", "payment_name"],
        )


def _unscope_adsense_payment_unique_constraint() -> None:
    with op.batch_alter_table("adsense_payments") as batch_op:
        batch_op.drop_constraint("uq_adsense_payments_month_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_adsense_payments_month_name",
            ["month", "payment_name"],
        )


def _scope_org_units_parent_fk() -> None:
    # ============================================================================
    # Purpose: Scope the org_units self-referential parent FK to be tenant-aware.
    #          A (tenant_id, parent_id) -> (tenant_id, id) composite FK prevents
    #          a child org_unit from referencing a parent in a different tenant.
    # Database/ORM: org_units, OrgUnitORM.
    # Standards: batch_alter_table for SQLite compat; unique constraint on
    #            (tenant_id, id) is the composite FK target.
    # Blast Radius: org hierarchy cross-tenant references now blocked at DB level.
    # ============================================================================
    with op.batch_alter_table("org_units") as batch_op:
        batch_op.create_unique_constraint(
            "uq_org_units_tenant_id_id", ["tenant_id", "id"]
        )
        batch_op.drop_constraint("org_units_parent_id_fkey", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_org_units_tenant_parent",
            "org_units",
            ["tenant_id", "parent_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )


def _unscope_org_units_parent_fk() -> None:
    with op.batch_alter_table("org_units") as batch_op:
        batch_op.drop_constraint("fk_org_units_tenant_parent", type_="foreignkey")
        batch_op.drop_constraint("uq_org_units_tenant_id_id", type_="unique")
        batch_op.create_foreign_key(
            "org_units_parent_id_fkey",
            "org_units",
            ["parent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
