"""Model-parity tests for the tenants foundation schema."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from ums_smart_revenue.db.tenant_models import (
    PLATFORM_ADMIN_STATUS_VALUES,
    TENANT_STATUS_VALUES,
    TenantBase,
)


def test_tenant_metadata_contains_required_tables():
    assert set(TenantBase.metadata.tables) >= {"tenants", "platform_admins"}


def test_tenants_table_columns_and_constraints():
    table = TenantBase.metadata.tables["tenants"]

    expected_columns = {
        "id",
        "slug",
        "display_name",
        "primary_currency",
        "status",
        "onboarding_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns <= set(table.columns.keys())

    not_null_columns = {
        column.name for column in table.columns if not column.nullable
    }
    assert expected_columns <= not_null_columns

    constraint_names = {constraint.name for constraint in table.constraints}
    assert {
        "ck_tenants_slug_lower",
        "ck_tenants_status",
        "ck_tenants_primary_currency_iso4217",
    } <= constraint_names

    index_names = {index.name for index in table.indexes}
    assert "uq_tenants_slug" in index_names


def test_platform_admins_table_columns_and_constraints():
    table = TenantBase.metadata.tables["platform_admins"]

    expected_columns = {
        "id",
        "email",
        "display_name",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns <= set(table.columns.keys())

    not_null_columns = {
        column.name for column in table.columns if not column.nullable
    }
    assert expected_columns <= not_null_columns

    constraint_names = {constraint.name for constraint in table.constraints}
    assert "ck_platform_admins_status" in constraint_names

    index_names = {index.name for index in table.indexes}
    assert "uq_platform_admins_email_lower" in index_names


def test_tenant_status_vocabulary_matches_constraint():
    assert TENANT_STATUS_VALUES == ("ACTIVE", "SUSPENDED", "ARCHIVED")


def test_platform_admin_status_vocabulary_matches_constraint():
    assert PLATFORM_ADMIN_STATUS_VALUES == ("ACTIVE", "SUSPENDED", "RETIRED")


def test_postgresql_ddl_renders_required_constraints():
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for table in TenantBase.metadata.sorted_tables
    )

    assert "tenants" in ddl
    assert "platform_admins" in ddl
    assert "ck_tenants_slug_lower" in ddl
    assert "ck_tenants_status" in ddl
    assert "ck_tenants_primary_currency_iso4217" in ddl
    assert "ck_platform_admins_status" in ddl
