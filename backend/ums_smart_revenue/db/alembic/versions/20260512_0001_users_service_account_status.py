"""Constrain user service-account status combinations.

Revision ID: 20260512_0001
Revises: 20260511_0001
Create Date: 2026-05-12
"""

from alembic import op

revision = "20260512_0001"
down_revision = "20260511_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_users_service_account_status",
        "users",
        "(is_service_account = true AND status IN ('service', 'disabled')) "
        "OR (is_service_account = false AND status IN ('active', 'disabled'))",
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop the service-account status check."""
    op.drop_constraint(
        "ck_users_service_account_status",
        "users",
        type_="check",
    )
