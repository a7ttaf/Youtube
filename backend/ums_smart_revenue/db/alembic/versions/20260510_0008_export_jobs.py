"""Create export job table.

Revision ID: 20260510_0008
Revises: 20260510_0007
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0008"
down_revision = "20260510_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("export_type", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'QUEUED'")),
        sa.Column("file_url", sa.Text(), nullable=True),
        sa.Column("month_lock_status", sa.Text(), nullable=False, server_default=sa.text("'OPEN'")),
        sa.Column("include_confidence_notes", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("include_manual_override_notes", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "export_type IN ('FINANCE_EXCEL', 'EXECUTIVE_PDF', 'BRANDED_SLIDE_PACK', 'ANALYTICS_SUMMARY_CSV')",
            name="ck_export_jobs_export_type",
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', 'group')",
            name="ck_export_jobs_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) OR (scope_type <> 'global' AND scope_id IS NOT NULL)",
            name="ck_export_jobs_scope",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_export_jobs_month_format",
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_export_jobs_currency_usd"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_export_jobs_status",
        ),
        sa.CheckConstraint("month_lock_status IN ('OPEN', 'LOCKED')", name="ck_export_jobs_month_lock_status"),
    )
    op.create_index("ix_export_jobs_requested_by_created", "export_jobs", ["requested_by", "created_at"])
    op.create_index("ix_export_jobs_scope_month", "export_jobs", ["scope_type", "scope_id", "month"])


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created in this migration in reverse dependency order."""
    op.drop_index("ix_export_jobs_scope_month", table_name="export_jobs")
    op.drop_index("ix_export_jobs_requested_by_created", table_name="export_jobs")
    op.drop_table("export_jobs")
