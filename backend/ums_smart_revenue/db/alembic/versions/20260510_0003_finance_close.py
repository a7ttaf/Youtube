"""Create finance close control tables.

Revision ID: 20260510_0003
Revises: 20260510_0002
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0003"
down_revision = "20260510_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finance_month_close",
        sa.Column("month", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'OPEN'")),
        sa.Column("allocation_method", sa.Text(), nullable=True),
        sa.Column("allocation_rule_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("locked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlocked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_finance_month_close_month_format",
        ),
        sa.CheckConstraint("status IN ('OPEN', 'LOCKED')", name="ck_finance_month_close_status"),
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created in this migration in reverse dependency order."""
    op.drop_table("finance_month_close")
