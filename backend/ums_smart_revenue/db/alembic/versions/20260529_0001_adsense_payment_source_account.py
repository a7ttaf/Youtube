"""Add source_account_id to adsense_payments and re-key uniqueness.

Revision ID: 20260529_0001
Revises: 20260527_0001
Create Date: 2026-05-29
"""

import sqlalchemy as sa
from alembic import op

revision = "20260529_0001"
down_revision = "20260527_0001"  # current head, verified via `alembic heads`
branch_labels = None
depends_on = None


# ============================================================================
# Purpose: Make the live adsense_payments schema match AdSensePaymentORM —
#          add a NOT NULL source_account_id, enforce a non-empty CHECK, and
#          re-key the uniqueness from (tenant_id, month, payment_name) to
#          (tenant_id, source_account_id, month, payment_name) so the same
#          payment name can recur across distinct AdSense source accounts.
# Database/ORM: adsense_payments / AdSensePaymentORM (finance_models.py).
# Standards: batch_alter_table keeps the migration SQLite-compatible; on
#            Postgres these translate to direct ALTERs. nullable add ->
#            sentinel backfill -> NOT NULL so the column can be enforced
#            against pre-alpha rows without a destructive table rewrite.
# Blast Radius: Finance source-of-truth uniqueness key changes; no auth/audit/
#            Neo4j impact. PostgreSQL remains the finance source of truth.
# Connections:
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM contract
#     this migration must match (column, uq + ck constraint names).
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260517_0001_tenant_id_on_operational_tables.py -> created the prior
#     (tenant_id, month, payment_name) form of uq_adsense_payments_month_name.
# ============================================================================
def upgrade() -> None:
    # 1. Add nullable so existing pre-alpha rows can be backfilled.
    with op.batch_alter_table("adsense_payments") as batch:
        batch.add_column(sa.Column("source_account_id", sa.Text(), nullable=True))
    # 2. Backfill existing rows (manual uploads predating the live pull).
    op.execute(
        "UPDATE adsense_payments SET source_account_id = '__legacy_manual__' "
        "WHERE source_account_id IS NULL"
    )
    # 3. Enforce NOT NULL + non-empty, and re-key uniqueness to include account.
    with op.batch_alter_table("adsense_payments") as batch:
        batch.alter_column(
            "source_account_id", existing_type=sa.Text(), nullable=False
        )
        batch.create_check_constraint(
            "ck_adsense_payments_source_account_id_nonempty",
            "length(source_account_id) >= 1",
        )
        batch.drop_constraint("uq_adsense_payments_month_name", type_="unique")
        batch.create_unique_constraint(
            "uq_adsense_payments_account_month_name",
            ["tenant_id", "source_account_id", "month", "payment_name"],
        )


def downgrade() -> None:
    with op.batch_alter_table("adsense_payments") as batch:
        batch.drop_constraint(
            "uq_adsense_payments_account_month_name", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_adsense_payments_month_name",
            ["tenant_id", "month", "payment_name"],
        )
        batch.drop_constraint(
            "ck_adsense_payments_source_account_id_nonempty", type_="check"
        )
        batch.drop_column("source_account_id")
