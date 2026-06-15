"""Create tenants foundation tables.

Revision ID: 20260516_0001
Revises: 20260513_0001
Create Date: 2026-05-16

S2.1 — first slice of the multi-tenant rollout. Creates the registry of
tenants (UMS seeded as tenant #1) and the separate platform_admins
identity table. Does NOT add tenant_id columns to any existing operational
tables — that comes in S2.4 along with Row-Level Security policies.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260516_0001"
down_revision = "20260513_0001"
branch_labels = None
depends_on = None


# UMS is seeded as tenant #1 with a deterministic UUID so subsequent
# migrations and seed data can reference it without a lookup.
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "primary_currency",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
        ),
        sa.Column(
            "onboarding_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.CheckConstraint("slug = lower(slug)", name="ck_tenants_slug_lower"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_tenants_status",
        ),
        sa.CheckConstraint(
            "length(primary_currency) = 3 AND primary_currency = upper(primary_currency)",
            name="ck_tenants_primary_currency_iso4217",
        ),
    )
    op.create_index(
        "uq_tenants_slug",
        "tenants",
        ["slug"],
        unique=True,
    )

    op.create_table(
        "platform_admins",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
        ),
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
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'RETIRED')",
            name="ck_platform_admins_status",
        ),
    )
    op.create_index(
        "uq_platform_admins_email_lower",
        "platform_admins",
        [sa.text("lower(email)")],
        unique=True,
    )

    # ============================================================================
    # Purpose: Seed the deterministic default UMS tenant during tenant bootstrap.
    # Database/ORM: tenants table.
    # Standards: Parameterized SQL, migration-owned database write, SQLite-safe tests.
    # Blast Radius: Tenant registry seed only; no finance, audit, Neo4j, or exports.
    # Connections:
    #   - File: tests/db/test_tenants_migration.py -> Verifies SQLite migration tests.
    #   - File: backend/ums_smart_revenue/db/rls.py -> RLS depends on tenant rows.
    # ============================================================================
    bind = op.get_bind()
    tenant_seed_sql = (
        "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
        "VALUES (CAST(:tenant_id AS uuid), 'ums', 'UMS', 'USD', 'ACTIVE') "
        "ON CONFLICT (slug) DO NOTHING"
        if bind.dialect.name == "postgresql"
        else "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
        "VALUES (:tenant_id, 'ums', 'UMS', 'USD', 'ACTIVE') "
        "ON CONFLICT (slug) DO NOTHING"
    )
    op.execute(
        sa.text(tenant_seed_sql).bindparams(tenant_id=UMS_TENANT_ID)
    )


def downgrade() -> None:
    op.drop_index("uq_platform_admins_email_lower", table_name="platform_admins")
    op.drop_table("platform_admins")
    op.drop_index("uq_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
