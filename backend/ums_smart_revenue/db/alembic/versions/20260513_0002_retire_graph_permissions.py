"""Retire graph permissions and graph-read scopes.

Revision ID: 20260513_0002
Revises: 20260513_0001
Create Date: 2026-05-13
"""

from alembic import op, util

revision = "20260513_0002"
down_revision = "20260513_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM user_role_assignments
        WHERE scope_id IN (
            SELECT id FROM access_scopes WHERE scope_type = 'graph-read'
        )
        """
    )
    op.execute(
        """
        DELETE FROM user_permission_grants
        WHERE scope_id IN (
            SELECT id FROM access_scopes WHERE scope_type = 'graph-read'
        )
           OR permission_key IN ('graph.view', 'graph.view_finance')
        """
    )
    op.execute(
        """
        DELETE FROM role_permission_assignments
        WHERE permission_key IN ('graph.view', 'graph.view_finance')
        """
    )
    op.execute(
        "DELETE FROM permissions WHERE key IN ('graph.view', 'graph.view_finance')"
    )
    op.execute("DELETE FROM access_scopes WHERE scope_type = 'graph-read'")

    op.drop_constraint(
        "ck_access_scopes_scope_type",
        "access_scopes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_access_scopes_scope_type",
        "access_scopes",
        "scope_type IN ('global', 'sector', 'company', 'channel', "
        "'finance-month', 'export', 'connector')",
    )


def downgrade() -> None:
    raise util.CommandError(
        "Irreversible migration: upgrade() deletes graph-read scopes and grants; "
        "restore from backup if rollback is required."
    )
