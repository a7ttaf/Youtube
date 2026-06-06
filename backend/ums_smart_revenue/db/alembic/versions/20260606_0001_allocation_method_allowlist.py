"""Expand the committed_allocation_runs method allowlist to five methods.

Revision ID: 20260606_0001
Revises: 20260603_0001
Create Date: 2026-06-06

company_level + no_allocation ship in this PR; 'manual' is pre-cleared at the
DB layer for the paired manual-allocation PR so it needs no second migration
(the service-layer allowlist stays the authority and still rejects manual
until that PR lands).
"""

import sqlalchemy as sa
from alembic import op

revision = "20260606_0001"
down_revision = "20260603_0001"
branch_labels = None
depends_on = None

_NEW_METHODS = ("company_level", "manual", "no_allocation")


# ============================================================================
# Purpose: Widen the committed_allocation_runs allocation_method CHECK from the
#   two proportional methods to the full five-method set (gross, post_tax,
#   company_level, manual, no_allocation). DB CHECK is the backstop; the
#   service-layer COMMITTABLE_ALLOCATION_METHODS allowlist remains the
#   fail-closed authority for what actually commits.
# Database/ORM: committed_allocation_runs / CommittedAllocationRunORM
#   (ck_committed_allocation_runs_method only; no column/data change).
# Standards: batch_alter_table keeps the CHECK swap SQLite-compatible (direct
#   ALTER on Postgres); downgrade fails fast with an explicit message when rows
#   using a widened method exist (mirrors 20260603_0001).
# Blast Radius: Finance write schema; widening only — no existing row violates
#   the new CHECK. PostgreSQL remains source of truth. No auth/audit/Neo4j.
# ============================================================================
def upgrade() -> None:
    """Expand the runs method allowlist CHECK to the five-method set."""
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method IN ("
            "'gross_revenue_proportional', 'post_tax_revenue_proportional', "
            "'company_level', 'manual', 'no_allocation')",
        )


def downgrade() -> None:
    """Restore the two-method (gross + post_tax) CHECK.

    Precondition: any committed_allocation_runs rows using company_level,
    manual, or no_allocation must be removed first. PostgreSQL validates ALL
    existing rows when ADD CONSTRAINT runs without NOT VALID, so violating rows
    abort the downgrade with a cryptic constraint error — fail fast instead.

    :raises RuntimeError: If any ``committed_allocation_runs`` rows use one of
        the widened methods and have not been removed first.
    """
    if op.get_bind().dialect.name == "postgresql":
        count = op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM committed_allocation_runs "
                "WHERE allocation_method IN "
                "('company_level', 'manual', 'no_allocation')"
            )
        ).scalar()
        if count:
            raise RuntimeError(
                f"Cannot downgrade 20260606_0001: {count} committed_allocation_runs "
                "row(s) use company_level/manual/no_allocation. Remove them first."
            )
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method IN "
            "('gross_revenue_proportional', 'post_tax_revenue_proportional')",
        )
