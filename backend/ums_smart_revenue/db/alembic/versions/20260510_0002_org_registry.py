"""Create organization and channel registry tables.

Revision ID: 20260510_0002
Revises: 20260510_0001
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0002"
down_revision = "20260510_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("org_units.id", ondelete="RESTRICT")),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('HOLDING', 'SECTOR', 'COMPANY')", name="ck_org_units_type"),
    )
    op.create_index("ix_org_units_parent_id", "org_units", ["parent_id"])

    op.create_table(
        "youtube_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False, unique=True),
        sa.Column("channel_name", sa.Text(), nullable=False),
        sa.Column(
            "primary_org_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("org_units.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("cms_status", sa.Text(), nullable=False, server_default=sa.text("'UNKNOWN'")),
        sa.Column("content_owner_id", sa.Text(), nullable=True),
        sa.Column("revenue_required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "revenue_source_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'MISSING_REVENUE_SOURCE'"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cms_status IN ('INSIDE_CMS', 'OUTSIDE_CMS', 'UNKNOWN')", name="ck_youtube_channels_cms_status"),
        sa.CheckConstraint(
            "revenue_source_status IN ("
            "'OFFICIAL_CMS_REVENUE', "
            "'OFFICIAL_MANUAL_IMPORT', "
            "'ALLOCATED_FROM_PAYMENT_POOL', "
            "'PERFORMANCE_ONLY', "
            "'MISSING_REVENUE_SOURCE'"
            ")",
            name="ck_youtube_channels_revenue_source_status",
        ),
    )
    op.create_index("ix_youtube_channels_primary_org_unit_id", "youtube_channels", ["primary_org_unit_id"])

    op.create_table(
        "channel_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("group_type", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "group_type IN ("
            "'HOLDING', 'SECTOR', 'COMPANY', 'TV_BRAND', 'NEWS_BRAND', "
            "'CUSTOM_GROUP', 'FINANCE_GROUP', 'SEASONAL_GROUP'"
            ")",
            name="ck_channel_groups_group_type",
        ),
    )

    op.create_table(
        "channel_group_members",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("youtube_channels.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created in this migration in reverse dependency order."""
    op.drop_table("channel_group_members")
    op.drop_table("channel_groups")
    op.drop_index("ix_youtube_channels_primary_org_unit_id", table_name="youtube_channels")
    op.drop_table("youtube_channels")
    op.drop_index("ix_org_units_parent_id", table_name="org_units")
    op.drop_table("org_units")

