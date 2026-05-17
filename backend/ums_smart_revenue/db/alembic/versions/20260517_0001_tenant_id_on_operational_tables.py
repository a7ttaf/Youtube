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

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


revision = "20260517_0001"
down_revision = "20260516_0001"
branch_labels = None
depends_on = None


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


def downgrade() -> None:
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
                postgresql.UUID(as_uuid=True),
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
