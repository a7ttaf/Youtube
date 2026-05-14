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
        sa.Column("scope_channel_ids", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("export_jobs", "scope_channel_ids")
