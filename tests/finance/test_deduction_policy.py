from ums_smart_revenue.finance import deduction_policy, net_revenue


def test_deduction_policy_holds_net_policy_constants():
    """The neutral module is the source of truth for the two net-policy constants."""
    assert deduction_policy.SOURCE_SYSTEM_TO_SOURCE_KIND == {
        "adsense_management": "ADSENSE",
        "youtube_reporting": "YOUTUBE_CMS",
        "youtube_analytics": "YOUTUBE_ANALYTICS",
    }
    assert deduction_policy.NET_APPLICABLE_COMPONENT_KINDS == frozenset({"TAX", "DEDUCTION"})


def test_net_revenue_reexports_same_objects():
    """net_revenue MUST re-export the same constant objects for back-compat."""
    assert net_revenue.SOURCE_SYSTEM_TO_SOURCE_KIND is deduction_policy.SOURCE_SYSTEM_TO_SOURCE_KIND
    assert net_revenue.NET_APPLICABLE_COMPONENT_KINDS is deduction_policy.NET_APPLICABLE_COMPONENT_KINDS


def test_net_revenue_and_allocation_import_together_no_cycle():
    """Importing both modules together must not raise (cycle is gone)."""
    import importlib

    importlib.import_module("ums_smart_revenue.finance.net_revenue")
    importlib.import_module("ums_smart_revenue.finance.allocation")
