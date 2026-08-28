# ============================================================================
# Purpose: Add external identity mapping, user home org scope, and effective-dated
#   US withholding display-rate configuration (Docs/23 A6/A7, Docs/24 U3).
# Database/ORM: users.home_org_unit_id; external_identities;
#   us_withholding_rate_configs.
# Standards: Additive nullable home scope; tenant-scoped tables with RLS;
#   rate bounded 0..0.30 with no default row seeded.
# Blast Radius: Authorization identity resolution and display-estimate config only.
# Connections:
#   - File: backend/ums_smart_revenue/auth/external_identities.py
#   - File: backend/ums_smart_revenue/finance/us_withholding_config.py
# ============================================================================
"""External identity, home org scope, and US withholding config.

Revision ID: 20260828_0001
Revises: 20260805_0001
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "20260828_0001"
down_revision = "20260805_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("home_org_unit_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_users_tenant_home_org_unit",
            "org_units",
            ["tenant_id", "home_org_unit_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_users_home_org_unit_id",
            ["tenant_id", "home_org_unit_id"],
        )

    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("normalized_email", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_external_identities_tenant_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_subject",
            name="uq_external_identities_provider_subject",
        ),
    )
    op.create_index(
        "ix_external_identities_tenant_user_id",
        "external_identities",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "uq_external_identities_tenant_email_lower",
        "external_identities",
        ["tenant_id", sa.text("lower(normalized_email)")],
        unique=True,
    )

    op.create_table(
        "us_withholding_rate_configs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_type IN ('business', 'individual')",
            name="ck_us_withholding_rate_configs_account_type",
        ),
        sa.CheckConstraint(
            "rate >= 0 AND rate <= 0.30",
            name="ck_us_withholding_rate_configs_rate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "confirmed_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_us_withholding_rate_configs_confirmed_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_us_withholding_rate_configs_tenant_effective",
        "us_withholding_rate_configs",
        ["tenant_id", "effective_from"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_us_withholding_rate_configs_tenant_effective",
        table_name="us_withholding_rate_configs",
    )
    op.drop_table("us_withholding_rate_configs")
    op.drop_index(
        "uq_external_identities_tenant_email_lower",
        table_name="external_identities",
    )
    op.drop_index(
        "ix_external_identities_tenant_user_id",
        table_name="external_identities",
    )
    op.drop_table("external_identities")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_home_org_unit_id")
        batch.drop_constraint("fk_users_tenant_home_org_unit", type_="foreignkey")
        batch.drop_column("home_org_unit_id")
