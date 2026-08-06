# ============================================================================
# Purpose: Add the additive, nullable channel_groups.content_owner_id column
#   that scopes a CMS-synced group to the content owner whose sync created it.
# Database/ORM: channel_groups (ChannelGroupORM mirror).
# Standards: Alembic-owned DDL; additive and nullable so existing groups
#   (including bulk-import-created CMS groups predating this column) stay
#   valid with content_owner_id NULL. No unique/foreign-key constraint: this
#   is a plain scoping column, not an identity key.
# Blast Radius: Channel grouping metadata only. No finance totals, no
#   allocation, no connector behaviour.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> ORM mirror.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
#     create_group / list_synced_groups read and write this column.
# ============================================================================
"""Add channel_groups.content_owner_id.

Revision ID: 20260805_0001
Revises: 20260803_0001
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20260805_0001"
down_revision = "20260803_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable content_owner_id column."""
    with op.batch_alter_table("channel_groups") as batch:
        batch.add_column(sa.Column("content_owner_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the content_owner_id column."""
    with op.batch_alter_table("channel_groups") as batch:
        batch.drop_column("content_owner_id")
