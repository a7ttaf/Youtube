"""Add official revenue format breakdown columns.

Revision ID: 20260513_0005
Revises: 20260513_0004
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260513_0005"
down_revision = "20260513_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "monthly_channel_revenue_facts",
        sa.Column("shorts_revenue_usd", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "monthly_channel_revenue_facts",
        sa.Column("longform_revenue_usd", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "monthly_channel_revenue_facts",
        sa.Column("subscription_revenue_usd", sa.Numeric(18, 6), nullable=True),
    )
    op.create_check_constraint(
        "ck_monthly_channel_revenue_facts_shorts_nonnegative",
        "monthly_channel_revenue_facts",
        "shorts_revenue_usd IS NULL OR shorts_revenue_usd >= 0",
    )
    op.create_check_constraint(
        "ck_monthly_channel_revenue_facts_longform_nonnegative",
        "monthly_channel_revenue_facts",
        "longform_revenue_usd IS NULL OR longform_revenue_usd >= 0",
    )
    op.create_check_constraint(
        "ck_monthly_channel_revenue_facts_subscription_nonnegative",
        "monthly_channel_revenue_facts",
        "subscription_revenue_usd IS NULL OR subscription_revenue_usd >= 0",
    )
    op.create_check_constraint(
        "ck_monthly_channel_revenue_facts_format_total",
        "monthly_channel_revenue_facts",
        "COALESCE(shorts_revenue_usd, 0) + "
        "COALESCE(longform_revenue_usd, 0) + "
        "COALESCE(subscription_revenue_usd, 0) <= gross_revenue_usd",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_monthly_channel_revenue_facts_format_total",
        "monthly_channel_revenue_facts",
        type_="check",
    )
    op.drop_constraint(
        "ck_monthly_channel_revenue_facts_subscription_nonnegative",
        "monthly_channel_revenue_facts",
        type_="check",
    )
    op.drop_constraint(
        "ck_monthly_channel_revenue_facts_longform_nonnegative",
        "monthly_channel_revenue_facts",
        type_="check",
    )
    op.drop_constraint(
        "ck_monthly_channel_revenue_facts_shorts_nonnegative",
        "monthly_channel_revenue_facts",
        type_="check",
    )
    op.drop_column("monthly_channel_revenue_facts", "subscription_revenue_usd")
    op.drop_column("monthly_channel_revenue_facts", "longform_revenue_usd")
    op.drop_column("monthly_channel_revenue_facts", "shorts_revenue_usd")
