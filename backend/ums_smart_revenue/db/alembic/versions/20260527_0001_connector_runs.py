"""Connector run tracking tables.

Revision ID: 20260527_0001
Revises: 20260523_0001
Create Date: 2026-05-27

Spec: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260527_0001"
down_revision = "20260523_0001"
branch_labels = None
depends_on = None

_COUNT_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_raw_report_files_tenant_id_id",
        "raw_report_files",
        ["tenant_id", "id"],
    )
    op.create_table(
        "connector_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("report_month", sa.Text(), nullable=False),
        sa.Column("triggered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'RUNNING'"),
        ),
        sa.Column(
            "counts_json",
            _COUNT_JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_connector_runs_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "triggered_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_connector_runs_triggered_by_user",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_connector_runs_tenant_id"),
        sa.CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_connector_runs_report_month_format",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')",
            name="ck_connector_runs_status",
        ),
        sa.CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 500",
            name="ck_connector_runs_error_summary_len",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_connector_runs_finished_at_order",
        ),
    )
    op.create_index(
        "ix_connector_runs_tenant_connector_month",
        "connector_runs",
        ["tenant_id", "connector_key", "report_month"],
    )
    op.create_index(
        "ix_connector_runs_tenant_started",
        "connector_runs",
        ["tenant_id", sa.text("started_at DESC")],
    )

    op.create_table(
        "connector_run_raw_files",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_run_id", sa.Uuid(), nullable=False),
        sa.Column("raw_report_file_id", sa.Uuid(), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ordering_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connector_run_id"],
            ["connector_runs.tenant_id", "connector_runs.id"],
            name="fk_connector_run_raw_files_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "raw_report_file_id"],
            ["raw_report_files.tenant_id", "raw_report_files.id"],
            name="fk_connector_run_raw_files_raw_file",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "connector_run_id",
            "raw_report_file_id",
            name="uq_connector_run_raw_files_run_file",
        ),
    )
    op.create_index(
        "ix_connector_run_raw_files_tenant_raw_file",
        "connector_run_raw_files",
        ["tenant_id", "raw_report_file_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_run_raw_files_tenant_raw_file",
        table_name="connector_run_raw_files",
    )
    op.drop_table("connector_run_raw_files")
    op.drop_index("ix_connector_runs_tenant_started", table_name="connector_runs")
    op.drop_index(
        "ix_connector_runs_tenant_connector_month",
        table_name="connector_runs",
    )
    op.drop_table("connector_runs")
    op.drop_constraint(
        "uq_raw_report_files_tenant_id_id",
        "raw_report_files",
        type_="unique",
    )
