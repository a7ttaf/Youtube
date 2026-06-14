from ums_smart_revenue.db.rls import (
    TENANT_SCOPED_TABLES,
    discover_tenant_tables_sql,
    tenant_rls_policy_name,
)


def test_allowlist_is_nonempty_and_excludes_platform_tables():
    """Validate the tenant-table allowlist covers scoped tables only."""
    assert "monthly_channel_revenue_facts" in TENANT_SCOPED_TABLES
    assert "google_revenue_source_rows" in TENANT_SCOPED_TABLES
    assert "tenants" not in TENANT_SCOPED_TABLES
    assert "currencies" not in TENANT_SCOPED_TABLES
    # No duplicates.
    assert len(TENANT_SCOPED_TABLES) == len(set(TENANT_SCOPED_TABLES))


def test_policy_name_is_table_scoped():
    """Confirm the policy name is derived directly from the table name."""
    assert tenant_rls_policy_name("adsense_payments") == ("adsense_payments_tenant_isolation")


def test_discover_sql_targets_tenant_id_columns():
    """Confirm the discovery SQL scans public tenant_id columns."""
    sql = discover_tenant_tables_sql()
    assert "information_schema.columns" in sql
    assert "tenant_id" in sql
