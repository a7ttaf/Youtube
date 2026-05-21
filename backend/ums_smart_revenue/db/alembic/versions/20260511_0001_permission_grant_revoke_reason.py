"""Add revoke_reason column to user_permission_grants.

Revision ID: 20260511_0001
Revises: 20260510_0008
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20260511_0001"
down_revision = "20260510_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_permission_grants",
        sa.Column("revoke_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop the revoke_reason column added
    in this migration."""
    op.drop_column("user_permission_grants", "revoke_reason")
