"""Create number explanation table.

Revision ID: 20260510_0007
Revises: 20260510_0006
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0007"
down_revision = "20260510_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "number_explanations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("components", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "month",
            "entity_type",
            "entity_id",
            "metric",
            name="uq_number_explanations_entity_metric_month",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_number_explanations_month_format",
        ),
        sa.CheckConstraint(
            "entity_type IN ('channel', 'company', 'sector', 'holding')",
            name="ck_number_explanations_entity_type",
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_number_explanations_currency_usd"),
    )
    op.create_index(
        "ix_number_explanations_entity",
        "number_explanations",
        ["entity_type", "entity_id", "month"],
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created in this migration in reverse dependency order."""
    op.drop_index("ix_number_explanations_entity", table_name="number_explanations")
    op.drop_table("number_explanations")
