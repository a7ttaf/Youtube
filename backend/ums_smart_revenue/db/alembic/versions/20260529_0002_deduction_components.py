"""Create deduction_components (source-reported deduction-evidence substrate).

Revision ID: 20260529_0002
Revises: 20260529_0001
Create Date: 2026-05-29

Spec: Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md (PR-A)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260529_0002"
down_revision = "20260529_0001"
branch_labels = None
depends_on = None

# Keep this migration self-contained with the UMS tenant seeded in
# 20260516_0001_tenants_foundation.py.
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# ============================================================================
# Purpose: Create the deduction_components substrate table that ingestion
#   populates with typed, source-labeled deduction evidence.
# Database/ORM: deduction_components / DeductionComponentORM (finance_models.py).
# Standards: idempotent on (tenant_id, component_key); finite NUMERIC + object
#   JSONB guards are Postgres-only (added via dialect check), mirroring
#   google_revenue_source_rows. Downgrade drops indexes then the table.
# Blast Radius: Finance source-of-truth (additive). PostgreSQL is source of
#   truth; no auth/audit/Neo4j schema impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM contract.
# ============================================================================
def upgrade() -> None:
    """Create deduction_components with constraints and indexes."""
    op.create_table(
        "deduction_components",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text(f"'{UMS_TENANT_ID}'"),
        ),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("amount_native", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency_code", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column(
            "raw_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
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
        sa.UniqueConstraint("tenant_id", "component_key", name="uq_deduction_components_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deduction_components_tenant",
            ondelete="RESTRICT",
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
            name="ck_deduction_components_month_format",
        ),
        sa.CheckConstraint(
            "component_kind IN ('TAX', 'DEDUCTION', 'TRANSFER_FEE', "
            "'FX_VARIANCE', 'UNRESOLVED_PAYMENT_GAP')",
            name="ck_deduction_components_kind",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('CHANNEL', 'ACCOUNT', 'PAYMENT')",
            name="ck_deduction_components_scope_kind",
        ),
        sa.CheckConstraint(
            "currency_code = 'USD'",
            name="ck_deduction_components_currency_code",
        ),
        sa.CheckConstraint(
            "length(scope_id) >= 1", name="ck_deduction_components_scope_id_nonempty"
        ),
        sa.CheckConstraint(
            "length(component_key) >= 1",
            name="ck_deduction_components_component_key_nonempty",
        ),
    )
    # Postgres-only guards (invalid SQLite CREATE TABLE syntax), mirroring the
    # ORM's .ddl_if(dialect="postgresql") CHECKs in finance_models.py.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_deduction_components_amount_usd_finite",
            "deduction_components",
            "amount_usd > '-Infinity'::numeric AND amount_usd < 'Infinity'::numeric",
        )
        op.create_check_constraint(
            "ck_deduction_components_amount_native_finite",
            "deduction_components",
            "amount_native IS NULL OR (amount_native > '-Infinity'::numeric "
            "AND amount_native < 'Infinity'::numeric)",
        )
        op.create_check_constraint(
            "ck_deduction_components_raw_payload_object",
            "deduction_components",
            "jsonb_typeof(raw_payload) = 'object'",
        )
    op.create_index(
        "ix_deduction_components_tenant_month",
        "deduction_components",
        ["tenant_id", "month"],
    )
    op.create_index(
        "ix_deduction_components_tenant_scope",
        "deduction_components",
        ["tenant_id", "scope_kind", "scope_id"],
    )
    op.create_index(
        "ix_deduction_components_tenant_month_kind",
        "deduction_components",
        ["tenant_id", "month", "component_kind"],
    )


def downgrade() -> None:
    """Drop deduction_components and its indexes."""
    op.drop_index(
        "ix_deduction_components_tenant_month_kind",
        table_name="deduction_components",
    )
    op.drop_index("ix_deduction_components_tenant_scope", table_name="deduction_components")
    op.drop_index("ix_deduction_components_tenant_month", table_name="deduction_components")
    op.drop_table("deduction_components")
