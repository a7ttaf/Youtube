"""Add PURGED status + purge audit columns to raw_report_files.

Revision ID: 20260609_0001
Revises: 20260608_0002
Create Date: 2026-06-09
"""

import sqlalchemy as sa
from alembic import op

revision = "20260609_0001"
down_revision = "20260608_0002"
branch_labels = None
depends_on = None

_OLD = "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED')"
_NEW = "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED', 'PURGED')"


def upgrade() -> None:
    """Add purged_at/purged_by columns and widen the parse_status CHECK."""
    with op.batch_alter_table("raw_report_files") as batch:
        batch.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("purged_by", sa.Uuid(as_uuid=True), nullable=True))
        batch.drop_constraint("ck_raw_report_files_parse_status", type_="check")
        batch.create_check_constraint("ck_raw_report_files_parse_status", _NEW)


def downgrade() -> None:
    """Restore the 4-value CHECK and drop the purge columns.

    :raises RuntimeError: if any PURGED rows exist (would violate old CHECK).
    """
    if op.get_bind().dialect.name == "postgresql":
        count = (
            op.get_bind()
            .execute(sa.text("SELECT COUNT(*) FROM raw_report_files WHERE parse_status = 'PURGED'"))
            .scalar()
        )
        if count:
            raise RuntimeError(f"Cannot downgrade: {count} PURGED raw_report_files rows exist.")
    with op.batch_alter_table("raw_report_files") as batch:
        batch.drop_constraint("ck_raw_report_files_parse_status", type_="check")
        batch.create_check_constraint("ck_raw_report_files_parse_status", _OLD)
        batch.drop_column("purged_by")
        batch.drop_column("purged_at")
