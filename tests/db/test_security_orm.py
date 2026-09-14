from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from ums_smart_revenue.db.security_models import SecurityBase


def test_security_orm_metadata_contains_required_tables():
    """SecurityBase registers every authz, audit, and credential table."""
    assert set(SecurityBase.metadata.tables) >= {
        "users",
        "roles",
        "permissions",
        "access_scopes",
        "role_permission_assignments",
        "user_role_assignments",
        "user_permission_grants",
        "audit_logs",
        "api_connector_credentials",
    }


def test_user_role_assignments_model_has_scope_and_revocation_controls():
    """user_role_assignments keeps scope, revocation, and index controls."""
    table = SecurityBase.metadata.tables["user_role_assignments"]

    assert {
        "tenant_id",
        "user_id",
        "role_key",
        "scope_id",
        "assigned_by",
        "revoked_by",
        "revoked_at",
        "reason",
        "active",
    } <= set(table.columns.keys())
    role_scope_index = next(
        (index for index in table.indexes if index.name == "uq_active_user_role_scope"),
        None,
    )
    assert role_scope_index is not None
    assert [column.name for column in role_scope_index.columns] == [
        "tenant_id",
        "user_id",
        "role_key",
        "scope_id",
    ]
    assert any(
        constraint.name == "fk_user_role_assignments_tenant_scope"
        for constraint in table.foreign_key_constraints
    )
    assert any(index.name == "ix_user_role_assignments_user_id" for index in table.indexes)


def test_postgresql_ddl_contains_sensitive_audit_and_connector_tables():
    """Compiled Postgres DDL keeps audit/credential tables and secret flags."""
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for table in SecurityBase.metadata.sorted_tables
    )

    assert "audit_logs" in ddl
    assert "sensitive BOOLEAN" in ddl
    assert "api_connector_credentials" in ddl
    assert "encrypted_secret_ref" in ddl
    assert "graph-read" not in ddl


def test_sqlite_global_access_scope_singleton_index_is_partial():
    """The global-scope singleton stays a partial unique index on SQLite."""
    table = SecurityBase.metadata.tables["access_scopes"]
    index = next(
        (index for index in table.indexes if index.name == "uq_access_scopes_global_singleton"),
        None,
    )
    assert index is not None

    ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))

    assert "CREATE UNIQUE INDEX uq_access_scopes_global_singleton" in ddl
    assert "WHERE scope_type = 'global' AND scope_id IS NULL" in ddl


def test_audit_logs_model_declares_request_lifecycle_index():
    """audit_logs carries the (tenant_id, event_type, request_id) index.

    Connector-job dispatch claiming, activation-failure dedupe, and startup
    recovery all filter audit_logs on that tuple (executor.py
    _lock_job_lifecycle_actions + the recovery anti-join); the index keeps
    those probes proportional to the lifecycle rows instead of scanning the
    tenant's full audit history. Migration 20260913_0001 creates it.
    """
    table = SecurityBase.metadata.tables["audit_logs"]
    index = next(
        (index for index in table.indexes if index.name == "ix_audit_logs_tenant_event_request"),
        None,
    )
    assert index is not None
    assert [column.name for column in index.columns] == [
        "tenant_id",
        "event_type",
        "request_id",
    ]
