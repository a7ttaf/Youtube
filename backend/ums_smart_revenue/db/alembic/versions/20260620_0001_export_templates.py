# ============================================================================
# Purpose: Define the additive export-template schema migration and tenant RLS.
# Database/ORM: export_templates table and export_jobs.template_id.
# Standards: Alembic-owned DDL, dialect-guarded RLS, and app-role grants.
# Blast Radius: Export configuration, tenant isolation, and historical jobs.
# Connections:
#   - File: backend/ums_smart_revenue/db/report_models.py -> ORM mirror.
#   - File: backend/ums_smart_revenue/db/rls.py -> Tenant isolation constants.
# ============================================================================
"""Add tenant-scoped export templates.

Revision ID: 20260620_0001
Revises: 20260618_0001
Create Date: 2026-06-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_CONTEXT_GETTER,
    tenant_rls_policy_name,
)

revision = "20260620_0001"
down_revision = "20260618_0001"
branch_labels = None
depends_on = None

_TENANT_ID_DEFAULT = sa.text("'00000000-0000-0000-0000-000000000001'")
_EXPORT_TEMPLATES_TABLE = "export_templates"


# ============================================================================
# Purpose: Add reusable export layout templates and nullable export-job
# references without changing historical rows or export calculations.
# Database/ORM: export_templates table and export_jobs.template_id.
# Standards: Additive migration, SQLite-compatible batch alter for the existing
# export_jobs table, and soft-delete-ready template rows.
# Blast Radius: Export configuration and audit only. No finance source rows,
# month close state, or authorization broadening.
# Connections:
#   - File: backend/ums_smart_revenue/db/report_models.py -> ORM mirror.
#   - File: backend/ums_smart_revenue/reports/exports.py -> Repository checks.
# ============================================================================
def upgrade() -> None:
    """Create export_templates and link export_jobs to active template choices."""
    op.create_table(
        "export_templates",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("export_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "layout_config",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
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
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=_TENANT_ID_DEFAULT,
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_export_templates_tenant_id_name"),
        # Composite (tenant_id, id) unique key is the target of the
        # export_jobs (tenant_id, template_id) foreign key, so a template
        # reference can never cross tenants at the DB level. id is the primary
        # key, so this is functionally redundant for uniqueness but required by
        # PostgreSQL as the explicit unique target of the composite FK.
        sa.UniqueConstraint("tenant_id", "id", name="uq_export_templates_tenant_id_id"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_export_templates_name_not_blank"),
        sa.CheckConstraint(
            "export_type IN ('FINANCE_EXCEL', 'EXECUTIVE_PDF', "
            "'BRANDED_SLIDE_PACK', 'ANALYTICS_SUMMARY_CSV')",
            name="ck_export_templates_export_type",
        ),
    )
    op.create_index(
        "ix_export_templates_tenant_type_active",
        "export_templates",
        ["tenant_id", "export_type", "is_active"],
    )
    op.create_index(
        "ix_export_templates_tenant_created",
        "export_templates",
        ["tenant_id", "created_at"],
    )
    _configure_export_templates_rls(op.get_bind())

    with op.batch_alter_table("export_jobs") as batch:
        batch.add_column(sa.Column("template_id", sa.Uuid(as_uuid=True), nullable=True))
        # Composite tenant-scoped FK so a template_id reference can never point
        # at another tenant's template at the DB level. NO ACTION (default)
        # blocks hard-deleting a template still referenced by a job; the
        # operational lifecycle is soft deactivation, which never trips this FK.
        batch.create_foreign_key(
            "fk_export_jobs_tenant_template",
            "export_templates",
            ["tenant_id", "template_id"],
            ["tenant_id", "id"],
        )
        batch.create_index("ix_export_jobs_template_id", ["template_id"])


def downgrade() -> None:
    """Remove template references and drop export_templates."""
    with op.batch_alter_table("export_jobs") as batch:
        batch.drop_index("ix_export_jobs_template_id")
        batch.drop_constraint("fk_export_jobs_tenant_template", type_="foreignkey")
        batch.drop_column("template_id")

    op.drop_index("ix_export_templates_tenant_created", table_name="export_templates")
    op.drop_index("ix_export_templates_tenant_type_active", table_name="export_templates")
    op.drop_table("export_templates")


# ============================================================================
# Purpose: Install tenant isolation for the new export_templates table in the
# same revision that creates it, including owner-bound FORCE RLS.
# Database/ORM: PostgreSQL public.export_templates; no SQLAlchemy ORM writes.
# Standards: Dialect-guarded, idempotent DROP/CREATE policy, and internal
# table/role constants only.
# Blast Radius: Authorization and export configuration isolation.
# Connections:
#   - File: backend/ums_smart_revenue/db/rls.py -> RLS roles and policy helper.
#   - File: tests/db/test_export_template_migration.py -> SQL contract coverage.
# ============================================================================
def _configure_export_templates_rls(bind) -> None:
    """Enable RLS, policy, FORCE, and app role grants for export templates."""
    if bind.dialect.name != "postgresql":
        return

    policy = tenant_rls_policy_name(_EXPORT_TEMPLATES_TABLE)
    bind.execute(
        sa.text(f'ALTER TABLE public."{_EXPORT_TEMPLATES_TABLE}" ENABLE ROW LEVEL SECURITY')
    )
    bind.execute(sa.text(f'DROP POLICY IF EXISTS {policy} ON public."{_EXPORT_TEMPLATES_TABLE}"'))
    bind.execute(
        sa.text(
            f'CREATE POLICY {policy} ON public."{_EXPORT_TEMPLATES_TABLE}" '
            f"USING (tenant_id = {TENANT_CONTEXT_GETTER}()) "
            f"WITH CHECK (tenant_id = {TENANT_CONTEXT_GETTER}())"
        )
    )
    bind.execute(
        sa.text(f'ALTER TABLE public."{_EXPORT_TEMPLATES_TABLE}" FORCE ROW LEVEL SECURITY')
    )
    bind.execute(
        sa.text(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{_EXPORT_TEMPLATES_TABLE}" '
            f'TO "{APP_TENANT_ROLE}"'
        )
    )
    bind.execute(
        sa.text(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{_EXPORT_TEMPLATES_TABLE}" '
            f'TO "{APP_PLATFORM_ROLE}"'
        )
    )
