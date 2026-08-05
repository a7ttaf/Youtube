# ============================================================================
# Purpose: Add the additive, nullable channel_groups.cms_group_id column that
#   links a UMS channel group to its YouTube CMS group key.
# Database/ORM: channel_groups (ChannelGroupORM mirror).
# Standards: Alembic-owned DDL; additive and nullable so existing groups stay
#   valid; unique per tenant only where a CMS key is present. batch_alter_table
#   on BOTH directions, matching the other constraint-changing migrations here:
#   SQLite cannot ALTER a constraint (NotImplementedError), so a direct
#   create_unique_constraint/drop_constraint would abort before head on every
#   local/disposable database. On PostgreSQL batch mode emits the same direct
#   ALTERs, so production DDL is unchanged.
# Blast Radius: Channel grouping metadata only. No finance totals, no
#   allocation, no connector behaviour.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> ORM mirror.
# ============================================================================
"""Add channel_groups.cms_group_id.

Revision ID: 20260803_0001
Revises: 20260620_0001
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "20260803_0001"
down_revision = "20260620_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable cms_group_id column and its per-tenant unique key."""
    # Two batches, not one: on SQLite each block is a copy-and-move table
    # rebuild, and adding the column in its own rebuild means the unique key is
    # declared against a table that already has it.
    with op.batch_alter_table("channel_groups") as batch:
        batch.add_column(sa.Column("cms_group_id", sa.Text(), nullable=True))
    with op.batch_alter_table("channel_groups") as batch:
        batch.create_unique_constraint(
            "uq_channel_groups_tenant_id_cms_group_id",
            ["tenant_id", "cms_group_id"],
        )


def downgrade() -> None:
    """Drop the per-tenant unique key and the cms_group_id column."""
    with op.batch_alter_table("channel_groups") as batch:
        batch.drop_constraint(
            "uq_channel_groups_tenant_id_cms_group_id",
            type_="unique",
        )
    with op.batch_alter_table("channel_groups") as batch:
        batch.drop_column("cms_group_id")
