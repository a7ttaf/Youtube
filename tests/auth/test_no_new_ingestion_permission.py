"""Permission catalog stays at the B1 baseline plus the approved P0-c grant.

Existing connectors.run_jobs covers ingestion job authorization. This
test pins the entire permission set against the PR #41 baseline plus the one
P0-c exception: manual revenue facts use a dedicated permission so beta users
do not receive global connector execution. The assertions intentionally guard
the full set, so any other permission drift remains visible.
"""

from ums_smart_revenue.auth.permissions import Permission

# Snapshot of the permission set as of PR #41 (post-merge baseline).
EXPECTED_PERMISSION_VALUES = frozenset(
    {
        "analytics.view",
        "analytics.view_confidence",
        "finance.view_revenue",
        "finance.view_finalized_payments",
        "finance.view_bank_reconciliation",
        "finance.manage_bank_reconciliation",
        "finance.create_manual_override",
        "finance.approve_manual_override",
        "finance.lock_month",
        "finance.unlock_month",
        "finance.change_allocation_rule",
        "exports.analytics",
        "exports.revenue",
        "exports.manage_templates",
        "registry.manage_channels",
        "registry.manage_org_mapping",
        "registry.manage_groups",
        "connectors.view_health",
        "connectors.run_jobs",
        "connectors.manage",
        "raw_files.view",
        "audit.view",
        "audit.view_sensitive_payloads",
        "users.manage",
        "roles.assign",
        "platform.manage_settings",
    }
)
P0_C_PERMISSION_VALUES = frozenset({"finance.import_manual_revenue"})


def test_permission_catalog_is_b1_baseline_plus_p0c_manual_revenue() -> None:
    """The permission catalog is exactly the B1 baseline plus P0-c manual revenue."""
    expected = EXPECTED_PERMISSION_VALUES | P0_C_PERMISSION_VALUES
    actual = {p.value for p in Permission}
    added = actual - expected
    removed = expected - actual
    assert not added, f"Unexpected permissions beyond the P0-c contract: {sorted(added)}"
    assert not removed, f"Expected baseline or P0-c permissions removed: {sorted(removed)}"
