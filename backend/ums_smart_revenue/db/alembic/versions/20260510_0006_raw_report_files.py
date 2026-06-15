"""Create raw report file metadata table.

Revision ID: 20260510_0006
Revises: 20260510_0005
Create Date: 2026-05-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260510_0006"
down_revision = "20260510_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_report_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("report_month", sa.Text(), nullable=False),
        sa.Column("file_url", sa.Text(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column(
            "parse_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'DOWNLOADED'"),
        ),
        sa.Column("downloaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "downloaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "source",
            "report_type",
            "report_month",
            "checksum",
            name="uq_raw_report_files_source_type_month_checksum",
        ),
        sa.CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_raw_report_files_report_month_format",
        ),
        sa.CheckConstraint(
            "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED')",
            name="ck_raw_report_files_parse_status",
        ),
    )
    op.create_index(
        "ix_raw_report_files_source_month",
        "raw_report_files",
        ["source", "report_month"],
    )
    op.create_index(
        "ix_raw_report_files_report_type_month",
        "raw_report_files",
        ["report_type", "report_month"],
    )


def downgrade() -> None:
    """Fully reverse upgrade(): drop all tables and indexes created
    in this migration in reverse dependency order."""
    op.drop_index("ix_raw_report_files_report_type_month", table_name="raw_report_files")
    op.drop_index("ix_raw_report_files_source_month", table_name="raw_report_files")
    op.drop_table("raw_report_files")
