"""Snapshot resolved channel ids on export jobs.

Revision ID: 20260513_0006
Revises: 20260513_0005
Create Date: 2026-05-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260513_0006"
down_revision = "20260513_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "export_jobs",
        sa.Column(
            "scope_channel_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
            comment="NULL = unresolved or global scope; [] = empty channel set; otherwise array of channel ID strings",
        ),
    )
    op.create_check_constraint(
        "ck_export_jobs_scope_channel_ids_is_array",
        "export_jobs",
        (
            "scope_channel_ids IS NULL OR ("
            "jsonb_typeof(scope_channel_ids) = 'array' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM jsonb_array_elements(scope_channel_ids) AS elem "
            "WHERE jsonb_typeof(elem) <> 'string'"
            "))"
        ),
    )
    op.create_index(
        "ix_export_jobs_scope_channel_ids",
        "export_jobs",
        ["scope_channel_ids"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_export_jobs_scope_channel_ids", table_name="export_jobs")
    op.drop_constraint(
        "ck_export_jobs_scope_channel_ids_is_array",
        "export_jobs",
        type_="check",
    )
    op.drop_column("export_jobs", "scope_channel_ids")
