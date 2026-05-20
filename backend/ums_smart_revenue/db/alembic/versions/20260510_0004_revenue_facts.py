"""Create monthly channel revenue fact table.

Revision ID: 20260510_0004
Revises: 20260510_0003
Create Date: 2026-05-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260510_0004"
down_revision = "20260510_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_channel_revenue_facts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column(
            "youtube_channel_id",
            sa.Text(),
            sa.ForeignKey(
                "youtube_channels.youtube_channel_id",
                name="fk_monthly_channel_revenue_facts_youtube_channel_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("gross_revenue_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("net_revenue_usd", sa.Numeric(18, 6), nullable=True),
        sa.Column(
            "views", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "watch_time_minutes",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "confidence_score",
            sa.Numeric(5, 4),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("imported_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "month",
            "youtube_channel_id",
            "source_kind",
            name="uq_monthly_channel_revenue_source",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_monthly_channel_revenue_facts_month_format",
        ),
        sa.CheckConstraint(
            "source_kind IN ('YOUTUBE_CMS', 'YOUTUBE_ANALYTICS', "
            "'ADSENSE', 'MANUAL_UPLOAD', 'ALLOCATION')",
            name="ck_monthly_channel_revenue_facts_source_kind",
        ),
        sa.CheckConstraint(
            "gross_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_gross_nonnegative",
        ),
        sa.CheckConstraint(
            "net_revenue_usd IS NULL OR net_revenue_usd >= 0",
            name="ck_monthly_channel_revenue_facts_net_nonnegative",
        ),
        sa.CheckConstraint(
            "views >= 0", name="ck_monthly_channel_revenue_facts_views_nonnegative"
        ),
        sa.CheckConstraint(
            "watch_time_minutes >= 0",
            name="ck_monthly_channel_revenue_facts_watch_time_nonnegative",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_monthly_channel_revenue_facts_confidence_range",
        ),
    )
    op.create_index(
        "ix_monthly_channel_revenue_facts_month",
        "monthly_channel_revenue_facts",
        ["month"],
    )
    op.create_index(
        "ix_monthly_channel_revenue_facts_channel_month",
        "monthly_channel_revenue_facts",
        ["youtube_channel_id", "month"],
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created
    in this migration in reverse dependency order."""
    op.drop_index(
        "ix_monthly_channel_revenue_facts_channel_month",
        table_name="monthly_channel_revenue_facts",
    )
    op.drop_index(
        "ix_monthly_channel_revenue_facts_month",
        table_name="monthly_channel_revenue_facts",
    )
    op.drop_table("monthly_channel_revenue_facts")
