"""Create committed account-allocation snapshot tables (Phase 4 Spec 2b).

Revision ID: 20260602_0001
Revises: 20260531_0001
Create Date: 2026-06-02

Spec: Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

revision = "20260602_0001"
down_revision = "20260531_0001"
branch_labels = None
depends_on = None


def _finite(column: str) -> str:
    """Postgres-only finite (non-NaN, non-Inf) guard for a numeric column."""
    return f"{column} > '-Infinity'::numeric AND {column} < 'Infinity'::numeric"


# ============================================================================
# Purpose: Create the four committed-allocation snapshot tables that persist a
#   committed account-allocation run (runs header + lines + unallocated +
#   notes) as an immutable point-in-time record.
# Database/ORM: committed_allocation_runs / committed_allocation_lines /
#   committed_allocation_unallocated / committed_allocation_notes
#   (CommittedAllocation* ORM in finance_models.py).
# Standards: finite NUMERIC CHECKs are Postgres-only (dialect guard), mirroring
#   deduction_components / finance_models.py .ddl_if(dialect="postgresql").
#   Downgrade drops indexes then tables (children before parent).
# Blast Radius: Finance write -- 4 new committed-allocation snapshot tables,
#   additive. No reader/auth/Neo4j schema impact. PostgreSQL source of truth.
# Connections:
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM contract.
#   - File: Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md
# ============================================================================
def upgrade() -> None:
    """Create the four committed-allocation tables with constraints + indexes."""
    op.create_table(
        "committed_allocation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=sa.text(f"'{UMS_TENANT_ID}'")),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("commit_version", sa.Integer(), nullable=False),
        sa.Column("allocation_method", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("allocated_component_count", sa.Integer(), nullable=False),
        sa.Column("unallocated_component_count", sa.Integer(), nullable=False),
        sa.Column("allocated_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("unallocated_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_applicable_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("reconciliation_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("committed_by", sa.Uuid(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_committed_allocation_runs_tenant", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "month", "commit_version",
            name="uq_committed_allocation_runs_version",
        ),
        sa.UniqueConstraint(
            "tenant_id", "month", "idempotency_key",
            name="uq_committed_allocation_runs_idempotency",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_committed_allocation_runs_month_format",
        ),
        sa.CheckConstraint(
            "allocation_method = 'gross_revenue_proportional'",
            name="ck_committed_allocation_runs_method",
        ),
        sa.CheckConstraint("commit_version >= 1", name="ck_committed_allocation_runs_version_positive"),
        sa.CheckConstraint("length(idempotency_key) >= 1", name="ck_committed_allocation_runs_idempotency_nonempty"),
        sa.CheckConstraint("length(reason) >= 1", name="ck_committed_allocation_runs_reason_nonempty"),
    )
    op.create_table(
        "committed_allocation_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("adsense_account_id", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("basis_source_kind", sa.Text(), nullable=False),
        sa.Column("basis_gross_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("basis_share", sa.Numeric(20, 6), nullable=False),
        sa.Column("allocated_amount_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_applicable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_lines_run", ondelete="CASCADE",
        ),
    )
    op.create_table(
        "committed_allocation_unallocated",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("issue_code", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_unallocated_run", ondelete="CASCADE",
        ),
    )
    op.create_table(
        "committed_allocation_notes",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("note_code", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_notes_run", ondelete="CASCADE",
        ),
    )
    # Postgres-only finite guards (invalid SQLite CREATE syntax), mirroring the
    # ORM's .ddl_if(dialect="postgresql") CHECKs in finance_models.py.
    if op.get_bind().dialect.name == "postgresql":
        for col in (
            "allocated_total_usd", "unallocated_total_usd",
            "net_applicable_total_usd", "reconciliation_total_usd",
        ):
            op.create_check_constraint(
                f"ck_committed_allocation_runs_{col}_finite",
                "committed_allocation_runs", _finite(col),
            )
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_gross_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )
    op.create_index(
        "ix_committed_allocation_runs_tenant_month",
        "committed_allocation_runs", ["tenant_id", "month"],
    )
    op.create_index(
        "ix_committed_allocation_lines_run", "committed_allocation_lines", ["run_id"]
    )
    op.create_index(
        "ix_committed_allocation_lines_run_channel",
        "committed_allocation_lines", ["run_id", "youtube_channel_id"],
    )
    op.create_index(
        "ix_committed_allocation_unallocated_run",
        "committed_allocation_unallocated", ["run_id"],
    )
    op.create_index(
        "ix_committed_allocation_notes_run", "committed_allocation_notes", ["run_id"]
    )


def downgrade() -> None:
    """Drop the four tables and their indexes (children first for FK safety)."""
    op.drop_index(
        "ix_committed_allocation_notes_run",
        table_name="committed_allocation_notes",
    )
    op.drop_table("committed_allocation_notes")
    op.drop_index(
        "ix_committed_allocation_unallocated_run",
        table_name="committed_allocation_unallocated",
    )
    op.drop_table("committed_allocation_unallocated")
    op.drop_index(
        "ix_committed_allocation_lines_run_channel",
        table_name="committed_allocation_lines",
    )
    op.drop_index(
        "ix_committed_allocation_lines_run",
        table_name="committed_allocation_lines",
    )
    op.drop_table("committed_allocation_lines")
    op.drop_index(
        "ix_committed_allocation_runs_tenant_month",
        table_name="committed_allocation_runs",
    )
    op.drop_table("committed_allocation_runs")
