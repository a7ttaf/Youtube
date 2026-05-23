"""B1 does not introduce a new permission for Google source-ingestion.

Existing connectors.run_jobs covers ingestion job authorization. This
test pins the contract: no new connectors.* or ingestion.* permission
appeared in this PR.
"""

from ums_smart_revenue.auth.permissions import Permission

# Snapshot of the permission set as of PR #41 (post-merge baseline).
EXPECTED_PERMISSION_VALUES = frozenset({
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
})


def test_no_new_permission_added_in_b1() -> None:
    actual = {p.value for p in Permission}
    added = actual - EXPECTED_PERMISSION_VALUES
    removed = EXPECTED_PERMISSION_VALUES - actual
    assert not added, f"B1 added unexpected permissions: {sorted(added)}"
    assert not removed, f"B1 removed permissions (out of scope): {sorted(removed)}"
