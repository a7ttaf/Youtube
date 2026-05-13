"""Add generated export artifact metadata.

Revision ID: 20260513_0003
Revises: 20260513_0002
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260513_0003"
down_revision = "20260513_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "export_jobs",
        sa.Column("artifact_filename", sa.Text(), nullable=True),
    )
    op.add_column(
        "export_jobs",
        sa.Column("artifact_content_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "export_jobs",
        sa.Column("artifact_byte_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "export_jobs",
        sa.Column("artifact_checksum_sha256", sa.Text(), nullable=True),
    )
    op.add_column(
        "export_jobs",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_export_jobs_artifact_byte_size",
        "export_jobs",
        "artifact_byte_size IS NULL OR artifact_byte_size >= 0",
    )
    op.create_check_constraint(
        "ck_export_jobs_artifact_checksum_sha256",
        "export_jobs",
        "artifact_checksum_sha256 IS NULL OR length(artifact_checksum_sha256) = 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_export_jobs_artifact_checksum_sha256",
        "export_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_export_jobs_artifact_byte_size",
        "export_jobs",
        type_="check",
    )
    op.drop_column("export_jobs", "failure_reason")
    op.drop_column("export_jobs", "artifact_checksum_sha256")
    op.drop_column("export_jobs", "artifact_byte_size")
    op.drop_column("export_jobs", "artifact_content_type")
    op.drop_column("export_jobs", "artifact_filename")
