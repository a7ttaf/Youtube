from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from ums_smart_revenue.db.security_models import SecurityBase


def test_security_orm_metadata_contains_required_tables():
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
    table = SecurityBase.metadata.tables["user_role_assignments"]

    assert {"user_id", "role_key", "scope_id", "assigned_by", "revoked_by", "revoked_at", "reason", "active"} <= set(
        table.columns.keys()
    )
    assert any(index.name == "uq_active_user_role_scope" for index in table.indexes)
    assert any(index.name == "ix_user_role_assignments_user_id" for index in table.indexes)


def test_postgresql_ddl_contains_sensitive_audit_and_connector_tables():
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for table in SecurityBase.metadata.sorted_tables
    )

    assert "audit_logs" in ddl
    assert "sensitive BOOLEAN" in ddl
    assert "api_connector_credentials" in ddl
    assert "encrypted_secret_ref" in ddl
