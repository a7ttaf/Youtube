"""Create security foundation tables.

Revision ID: 20260510_0001
Revises: None
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260510_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("is_service_account", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'disabled', 'service')", name="ck_users_status"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "roles",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("service_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "permissions",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("audit_on_use", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "access_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', 'finance-month', 'export', 'connector', 'graph-read')",
            name="ck_access_scopes_scope_type",
        ),
    )
    op.create_index(
        "uq_access_scopes_scope_type_scope_id",
        "access_scopes",
        ["scope_type", "scope_id"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NOT NULL"),
    )
    op.create_index(
        "uq_access_scopes_scope_type_null_scope_id",
        "access_scopes",
        ["scope_type"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NULL"),
    )

    op.create_table(
        "role_permission_assignments",
        sa.Column("role_key", sa.Text(), sa.ForeignKey("roles.key", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "permission_key",
            sa.Text(),
            sa.ForeignKey("permissions.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "user_role_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_key", sa.Text(), sa.ForeignKey("roles.key", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_scopes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(
            "(active = true AND revoked_at IS NULL) OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_role_assignments_revocation",
        ),
    )
    op.create_index(
        "uq_active_user_role_scope",
        "user_role_assignments",
        ["user_id", "role_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
    )

    op.create_table(
        "user_permission_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission_key", sa.Text(), sa.ForeignKey("permissions.key", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_scopes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(
            "(active = true AND revoked_at IS NULL) OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_permission_grants_revocation",
        ),
    )
    op.create_index(
        "uq_active_user_permission_scope",
        "user_permission_grants",
        ["user_id", "permission_key", "scope_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("scope_type", sa.Text(), nullable=True),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_user_created", "audit_logs", ["user_id", "created_at"])
    op.create_index("ix_audit_logs_event_created", "audit_logs", ["event_type", "created_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])

    op.create_table(
        "api_connector_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("encrypted_secret_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'disabled', 'rotating', 'failed_auth')", name="ck_connector_status"),
    )
    op.create_index(
        "uq_api_connector_credentials_connector_account",
        "api_connector_credentials",
        ["connector_key", "account_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_api_connector_credentials_connector_account", table_name="api_connector_credentials")
    op.drop_table("api_connector_credentials")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("uq_active_user_permission_scope", table_name="user_permission_grants")
    op.drop_table("user_permission_grants")
    op.drop_index("uq_active_user_role_scope", table_name="user_role_assignments")
    op.drop_table("user_role_assignments")
    op.drop_table("role_permission_assignments")
    op.drop_index("uq_access_scopes_scope_type_null_scope_id", table_name="access_scopes")
    op.drop_index("uq_access_scopes_scope_type_scope_id", table_name="access_scopes")
    op.drop_table("access_scopes")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users")

