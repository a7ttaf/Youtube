"""Shared Row-Level Security helpers: the canonical tenant-table allowlist and
the migration helpers that enable/disable tenant-isolation policies.

Used by the RLS enforcement migration and by the isolation drift guard. The
allowlist is the single source of truth for "which tables are tenant-scoped";
the migration cross-checks it against the live schema so a new tenant table that
ships without RLS becomes a loud migration failure, not a silent leak.
"""

from __future__ import annotations

# Every table carrying a tenant_id column (platform-wide tables excluded).
# Keep sorted; the migration asserts this equals the live information_schema set.
# Derived from a live metadata scan (every Declarative table with a tenant_id
# column, minus `tenants`); platform-wide `currencies` / `currency_exchange_rates`
# carry no tenant_id and are excluded.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "access_scopes",
    "adsense_content_owner_links",
    "adsense_payments",
    "api_connector_credentials",
    "audit_logs",
    "bank_reconciliation_entries",
    "channel_group_members",
    "channel_groups",
    "committed_allocation_runs",
    "connector_run_raw_files",
    "connector_runs",
    "content_owner_channel_links",
    "deduction_components",
    "export_jobs",
    "export_templates",
    "finance_month_close",
    "google_revenue_source_rows",
    "monthly_channel_revenue_facts",
    "number_explanations",
    "org_units",
    "raw_report_files",
    "revenue_manual_overrides",
    "user_permission_grants",
    "user_role_assignments",
    "users",
    "youtube_channels",
)

APP_TENANT_ROLE = "app_tenant"
APP_PLATFORM_ROLE = "app_platform"
# Trusted tenant-context helpers owned by the database, not by the tenant lane.
# The session hook writes the current backend's tenant row through a privileged
# setter; RLS policies read it back through the getter.
TENANT_CONTEXT_TABLE = "app_tenant_context"
TENANT_CONTEXT_SETTER = "set_app_current_tenant_id"
TENANT_CONTEXT_GETTER = "app_current_tenant_id"
# Privileged SECURITY DEFINER clearer: the app lanes only hold SELECT on the
# context table, so the session hook clears its row via this function (which runs
# as the function owner) instead of a raw DELETE that would permission-deny.
TENANT_CONTEXT_CLEARER = "clear_app_current_tenant_id"


def tenant_rls_policy_name(table: str) -> str:
    """Return the deterministic isolation-policy name for a tenant table."""
    return f"{table}_tenant_isolation"


def discover_tenant_tables_sql() -> str:
    """SQL returning every public table that has a tenant_id column (no tenants)."""
    return (
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
        "AND table_name <> 'tenants' ORDER BY table_name"
    )
