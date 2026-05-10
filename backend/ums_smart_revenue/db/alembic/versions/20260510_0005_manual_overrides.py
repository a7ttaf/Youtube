"""Create revenue manual override table.

Revision ID: 20260510_0005
Revises: 20260510_0004
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0005"
down_revision = "20260510_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revenue_manual_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column(
            "youtube_channel_id",
            sa.Text(),
            sa.ForeignKey(
                "youtube_channels.youtube_channel_id",
                name="fk_revenue_manual_overrides_youtube_channel_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("adjustment_revenue_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name="ck_revenue_manual_overrides_month_format",
        ),
        sa.CheckConstraint("adjustment_revenue_usd <> 0", name="ck_revenue_manual_overrides_adjustment_nonzero"),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_revenue_manual_overrides_status"),
        sa.CheckConstraint(
            "(status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL) "
            "OR (status <> 'APPROVED' AND approved_by IS NULL AND approved_at IS NULL)",
            name="ck_revenue_manual_overrides_approval_fields",
        ),
    )
    op.create_index(
        "ix_revenue_manual_overrides_month_status",
        "revenue_manual_overrides",
        ["month", "status"],
    )
    op.create_index(
        "ix_revenue_manual_overrides_channel_month",
        "revenue_manual_overrides",
        ["youtube_channel_id", "month"],
    )


def downgrade() -> None:
    op.drop_index("ix_revenue_manual_overrides_channel_month", table_name="revenue_manual_overrides")
    op.drop_index("ix_revenue_manual_overrides_month_status", table_name="revenue_manual_overrides")
    op.drop_table("revenue_manual_overrides")
