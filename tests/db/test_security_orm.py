from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from ums_smart_revenue.db.security_models import (
    ExternalIdentityORM,
    SecurityBase,
    UserORM,
    UsWithholdingRateConfigORM,
)


def test_security_orm_metadata_contains_required_tables():
    """Assert the security metadata declares every required table."""
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
        "external_identities",
        "us_withholding_rate_configs",
    }


def test_user_home_org_foreign_key_is_composite_and_tenant_scoped() -> None:
    """Assert the home-org foreign key is composite, tenant-scoped, and RESTRICT."""
    try:
        constraint = next(
            constraint
            for constraint in UserORM.__table__.foreign_key_constraints
            if constraint.name == "fk_users_tenant_home_org_unit"
        )
    except StopIteration:
        raise AssertionError("expected fk_users_tenant_home_org_unit foreign key") from None

    assert [column.name for column in constraint.columns] == [
        "tenant_id",
        "home_org_unit_id",
    ]
    assert [element.target_fullname for element in constraint.elements] == [
        "org_units.tenant_id",
        "org_units.id",
    ]
    assert constraint.ondelete == "RESTRICT"


def test_new_tenant_owned_foreign_keys_match_migration_contract() -> None:
    """Assert the new tenant-owned foreign keys match the migration contract."""
    expected = {
        ExternalIdentityORM: (
            "fk_external_identities_tenant_user",
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            "CASCADE",
        ),
        UsWithholdingRateConfigORM: (
            "fk_us_withholding_rate_configs_confirmed_by",
            ["tenant_id", "confirmed_by_user_id"],
            ["users.tenant_id", "users.id"],
            "RESTRICT",
        ),
    }

    for model, (name, local_columns, targets, ondelete) in expected.items():
        try:
            constraint = next(
                constraint
                for constraint in model.__table__.foreign_key_constraints
                if constraint.name == name
            )
        except StopIteration:
            raise AssertionError(f"expected {name} foreign key on {model.__name__}") from None
        assert [column.name for column in constraint.columns] == local_columns
        assert [element.target_fullname for element in constraint.elements] == targets
        assert constraint.ondelete == ondelete


def test_new_tenant_owned_models_have_no_silent_tenant_default() -> None:
    """Assert the new models expose no silent default on tenant-scoping columns."""
    for model in (ExternalIdentityORM, UsWithholdingRateConfigORM):
        tenant_column = model.__table__.c.tenant_id
        assert tenant_column.default is None
        assert tenant_column.server_default is None
    source_account_column = UsWithholdingRateConfigORM.__table__.c.source_account_id
    assert source_account_column.nullable is False
    assert source_account_column.default is None
    assert source_account_column.server_default is None


def test_withholding_effective_index_has_deterministic_tie_breakers() -> None:
    """Assert the effective index orders reads with revision DESC tie-breakers."""
    try:
        index = next(
            index
            for index in UsWithholdingRateConfigORM.__table__.indexes
            if index.name == "ix_us_withholding_rate_configs_tenant_effective"
        )
    except StopIteration:
        raise AssertionError(
            "expected ix_us_withholding_rate_configs_tenant_effective index"
        ) from None

    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "tenant_id, source_account_id, effective_from DESC, revision DESC" in ddl
    try:
        unique = next(
            constraint
            for constraint in UsWithholdingRateConfigORM.__table__.constraints
            if constraint.name == "uq_us_withholding_rate_configs_account_effective_revision"
        )
    except StopIteration:
        raise AssertionError(
            "expected uq_us_withholding_rate_configs_account_effective_revision constraint"
        ) from None
    assert [column.name for column in unique.columns] == [
        "tenant_id",
        "source_account_id",
        "effective_from",
        "revision",
    ]


def test_user_role_assignments_model_has_scope_and_revocation_controls():
    """Assert the assignment model carries scope and revocation controls."""
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
    """Assert the PostgreSQL DDL keeps sensitive audit and credential columns."""
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
    """Assert the global access-scope singleton index is partial on SQLite."""
    table = SecurityBase.metadata.tables["access_scopes"]
    index = next(
        (index for index in table.indexes if index.name == "uq_access_scopes_global_singleton"),
        None,
    )
    assert index is not None

    ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))

    assert "CREATE UNIQUE INDEX uq_access_scopes_global_singleton" in ddl
    assert "WHERE scope_type = 'global' AND scope_id IS NULL" in ddl
