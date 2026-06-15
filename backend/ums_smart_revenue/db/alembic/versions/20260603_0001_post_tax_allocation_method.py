"""Rename committed_allocation_lines.basis_gross_usd -> basis_amount_usd and
expand the committed_allocation_runs method allowlist to post_tax.

Revision ID: 20260603_0001
Revises: 20260602_0001
Create Date: 2026-06-03

Spec: Docs/superpowers/specs/2026-06-03-spec-post-tax-allocation-method-design.md
"""

import sqlalchemy as sa
from alembic import op

revision = "20260603_0001"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def _finite(column: str) -> str:
    """Postgres-only finite (non-NaN, non-Inf) guard for a numeric column."""
    return f"{column} > '-Infinity'::numeric AND {column} < 'Infinity'::numeric"


# ============================================================================
# Purpose: Rename the committed-allocation line basis column to the honest,
#   method-neutral basis_amount_usd (it stores net for post_tax lines), recreate
#   the Postgres-only finite CHECK so its expression references the new column,
#   and expand the committed_allocation_runs method allowlist CHECK to accept
#   both gross_revenue_proportional and post_tax_revenue_proportional.
# Database/ORM: committed_allocation_lines / CommittedAllocationLineORM;
#   committed_allocation_runs / CommittedAllocationRunORM (method CHECK).
# Standards: batch_alter_table keeps the rename + CHECK swap SQLite-compatible
#   (on Postgres they are direct ALTERs); the finite CHECK is Postgres-only
#   (dialect-guarded), matching finance_models.py .ddl_if(dialect="postgresql").
# Blast Radius: Finance write schema; column rename on pre-alpha data preserves
#   rows; the method-allowlist expansion only widens the accepted set (no
#   existing row violates it). PostgreSQL remains source of truth. No auth/audit/
#   Neo4j impact.
# ============================================================================
def upgrade() -> None:
    """Rename basis_gross_usd -> basis_amount_usd (+ recreate the finite CHECK) +
    expand the runs method allowlist CHECK to post_tax.
    """
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        op.drop_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            type_="check",
        )
    with op.batch_alter_table("committed_allocation_lines") as batch:
        batch.alter_column(
            "basis_gross_usd",
            new_column_name="basis_amount_usd",
            existing_type=sa.Numeric(20, 6),
            existing_nullable=False,
        )
    if is_pg:
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_amount_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )
    # Expand the runs method allowlist to gross + post_tax.
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method IN ('gross_revenue_proportional', 'post_tax_revenue_proportional')",
        )


def downgrade() -> None:
    """Restore the gross-only method CHECK + rename basis_amount_usd -> basis_gross_usd
    (+ recreate the finite CHECK).

    Precondition: any committed_allocation_runs rows with
    allocation_method='post_tax_revenue_proportional' must be removed before
    downgrading. PostgreSQL validates ALL existing rows when ADD CONSTRAINT runs
    without NOT VALID; rows that violate the restored gross-only CHECK will cause
    the downgrade to abort. This migration is therefore irreversible on databases
    that have accepted post_tax commits.

    :raises RuntimeError: If any ``committed_allocation_runs`` rows use
        ``post_tax_revenue_proportional`` and have not been removed first.
    """
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        # Fail fast before any DDL: a cryptic constraint-violation from PostgreSQL
        # is harder to diagnose than this explicit message.
        count = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT COUNT(*) FROM committed_allocation_runs "
                    "WHERE allocation_method = 'post_tax_revenue_proportional'"
                )
            )
            .scalar()
        )
        if count:
            raise RuntimeError(
                f"Cannot downgrade 20260603_0001: {count} committed_allocation_runs "
                "row(s) use post_tax_revenue_proportional. Remove them first."
            )
    # Restore the gross-only method CHECK.
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method = 'gross_revenue_proportional'",
        )
    if is_pg:
        op.drop_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            type_="check",
        )
    with op.batch_alter_table("committed_allocation_lines") as batch:
        batch.alter_column(
            "basis_amount_usd",
            new_column_name="basis_gross_usd",
            existing_type=sa.Numeric(20, 6),
            existing_nullable=False,
        )
    if is_pg:
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_gross_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )
