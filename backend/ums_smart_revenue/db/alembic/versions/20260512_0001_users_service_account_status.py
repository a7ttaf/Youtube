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
    """Backfill service-account status data and add the consistency check."""
    # Permanent canonicalization: these rows already violate the user lifecycle
    # contract, and reverting the repair would recreate invalid service-account
    # states after rollback.
    op.execute(
        "UPDATE users SET is_service_account = true "
        "WHERE status = 'service' AND is_service_account IS false"
    )
    op.execute(
        "UPDATE users SET status = 'service' "
        "WHERE is_service_account IS true AND status = 'active'"
    )
    op.create_check_constraint(
        "ck_users_service_account_status",
        "users",
        "(is_service_account = true AND status IN ('service', 'disabled')) "
        "OR (is_service_account = false AND status IN ('active', 'disabled'))",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    """Drop the constraint while preserving upgrade()'s data canonicalization."""
    op.drop_constraint(
        "ck_users_service_account_status",
        "users",
        type_="check",
    )
