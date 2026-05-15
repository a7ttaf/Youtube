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
            comment=(
                "NULL = unresolved or global scope; "
                "non-null = non-empty array of channel ID strings"
            ),
        ),
    )
    # Element-type enforcement (every entry must be a JSON string) is delegated
    # to the application's _normalize_scope_channel_ids in reports/exports.py.
    # Restricting the CHECK to shape + non-empty avoids embedding jsonpath
    # method-call syntax in a CHECK expression, where parser corner cases
    # across PostgreSQL versions have caused review debate.
    op.create_check_constraint(
        "ck_export_jobs_scope_channel_ids_is_array",
        "export_jobs",
        (
            "scope_channel_ids IS NULL OR ("
            "jsonb_typeof(scope_channel_ids) = 'array' "
            "AND jsonb_array_length(scope_channel_ids) > 0"
            ")"
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
