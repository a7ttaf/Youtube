# ============================================================================
# Purpose: Rename keyword-shaped and non-descriptive columns on the security
#   foundation tables to self-descriptive, unquoted names:
#     roles.key            -> roles.role_key
#     permissions.key      -> permissions.permission_key
#     permissions.sensitive -> permissions.is_sensitive
#     audit_logs.sensitive  -> audit_logs.is_sensitive
#   PostgreSQL RENAME COLUMN is metadata-only and automatically re-points the
#   dependent FK constraints (role_permission_assignments, user_role_assignments,
#   user_permission_grants) and PK/index definitions, so no constraint rebuild
#   is required and all existing rows are preserved in place.
# Database/ORM: roles, permissions, audit_logs (SecurityBase mirror in
#   security_models.py; bootstrap mirror in security_schema.sql).
# Standards: Alembic-owned DDL; fully reversible downgrade restores the old
#   names. Historical migrations (e.g. 20260510_0001, 20260513_0002) keep the
#   old names because they execute at their own position in the chain.
# Blast Radius: Authorization + audit substrate. ORM attribute names, the two
#   ORM consumers (sql_audit_sink, audit_log reader), the bootstrap SQL mirror,
#   and the seed SQL change in lockstep in the same PR. Public API/CSV/JSON
#   field names ("sensitive") are intentionally unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py -> ORM mirror.
#   - File: backend/ums_smart_revenue/db/security_schema.sql -> bootstrap DDL mirror.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> seed statements.
#   - File: tests/db/test_security_keyword_column_rename_migration_postgres.py ->
#     round-trip + data-preservation proof.
# ============================================================================
"""Rename keyword-shaped security columns to unquoted descriptive names.

Revision ID: 20260808_0001
Revises: 20260805_0001
Create Date: 2026-08-08
"""

from alembic import op

revision = "20260808_0001"
down_revision = "20260805_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("roles", "key", new_column_name="role_key")
    op.alter_column("permissions", "key", new_column_name="permission_key")
    op.alter_column("permissions", "sensitive", new_column_name="is_sensitive")
    op.alter_column("audit_logs", "sensitive", new_column_name="is_sensitive")


def downgrade() -> None:
    op.alter_column("audit_logs", "is_sensitive", new_column_name="sensitive")
    op.alter_column("permissions", "is_sensitive", new_column_name="sensitive")
    op.alter_column("permissions", "permission_key", new_column_name="key")
    op.alter_column("roles", "role_key", new_column_name="key")
