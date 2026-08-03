# ============================================================================
# Purpose: Add the additive, nullable channel_groups.cms_group_id column that
#   links a UMS channel group to its YouTube CMS group key.
# Database/ORM: channel_groups (ChannelGroupORM mirror).
# Standards: Alembic-owned DDL; additive and nullable so existing groups stay
#   valid; unique per tenant only where a CMS key is present.
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
    op.add_column(
        "channel_groups",
        sa.Column("cms_group_id", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_channel_groups_tenant_id_cms_group_id",
        "channel_groups",
        ["tenant_id", "cms_group_id"],
    )


def downgrade() -> None:
    """Drop the per-tenant unique key and the cms_group_id column."""
    op.drop_constraint(
        "uq_channel_groups_tenant_id_cms_group_id",
        "channel_groups",
        type_="unique",
    )
    op.drop_column("channel_groups", "cms_group_id")
