"""Add the source-row cursor index for tenant month pagination.

Revision ID: 20260608_0002
Revises: 20260608_0001
Create Date: 2026-06-08
"""

from alembic import op

revision = "20260608_0002"
down_revision = "20260608_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the composite index that backs newest-first month pagination."""
    op.create_index(
        "ix_google_revenue_source_rows_tenant_month_ingested_id",
        "google_revenue_source_rows",
        ["tenant_id", "report_month", "ingested_at", "id"],
    )


def downgrade() -> None:
    """Drop the source-row cursor index."""
    op.drop_index(
        "ix_google_revenue_source_rows_tenant_month_ingested_id",
        table_name="google_revenue_source_rows",
    )
