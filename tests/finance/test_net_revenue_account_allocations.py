from decimal import Decimal

from ums_smart_revenue.finance.allocation import AllocationLine, UnallocatedIssue
from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    build_month_net_revenue_summary,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH = "2026-04"
CH = "chA"


def _fact(*, source_kind="ADSENSE", gross="1000.00", net=None, channel=CH):
    """Build a RevenueFactEntry for test scenarios."""
    return RevenueFactEntry(
        id=f"f-{source_kind}-{channel}",
        month=MONTH,
        youtube_channel_id=channel,
        source_kind=source_kind,
        source_report_id=None,
        gross_revenue_usd=Decimal(gross),
        net_revenue_usd=(Decimal(net) if net is not None else None),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("0.9800"),
        imported_by=None,
    )


def _alloc(*, channel=CH, account="pub-1", kind="DEDUCTION", amount="100.000000",
           source_system="adsense_management", net_applicable=True, key="k1"):
    """Build an AllocationLine for test scenarios."""
    return AllocationLine(
        adsense_account_id=account,
        youtube_channel_id=channel,
        component_kind=kind,
        source_system=source_system,
        component_key=key,
        basis_source_kind="ADSENSE",
        basis_gross_usd=Decimal("1000.000000"),
        basis_share=Decimal("1.000000"),
        allocated_amount_usd=Decimal(amount),
        net_applicable=net_applicable,
    )


def _issue(*, account="pub-9", kind="DEDUCTION", amount="40.000000", code="ACCOUNT_UNMAPPED_OR_UNVERIFIED", key="u1"):
    """Build an UnallocatedIssue for test scenarios."""
    return UnallocatedIssue(
        scope_id=account, component_kind=kind, component_key=key,
        amount_usd=Decimal(amount), issue_code=code, detail="unmapped",
    )


def test_missing_net_applies_account_allocation():
    """Missing-net channel: net = adjusted_gross - account_allocated; breakdown set."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.net_revenue_usd == Decimal("900.000000")
    assert summary.deduction_amount_usd == Decimal("100.000000")
    assert summary.channel_direct_deduction_amount_usd == Decimal("0")
    assert summary.account_allocated_deduction_amount_usd == Decimal("100.000000")
    # sum identity holds on COMPONENT_DERIVED
    assert (
        summary.channel_direct_deduction_amount_usd
        + summary.account_allocated_deduction_amount_usd
        == summary.deduction_amount_usd
    )


def test_source_net_channel_ignores_account_allocation():
    """Source-net channel: net + deduction unchanged; breakdown fields are None."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net="880.00", gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.status == "CALCULATED"
    assert summary.net_revenue_usd == Decimal("880.00")
    assert summary.deduction_amount_usd == Decimal("120.00")  # 1000 - 880, source-derived
    assert summary.channel_direct_deduction_amount_usd is None
    assert summary.account_allocated_deduction_amount_usd is None


def test_source_alignment_blocks_cross_kind_allocation():
    """An adsense_management allocation does not reduce a YOUTUBE_CMS-primary net."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(source_kind="YOUTUBE_CMS", net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(source_system="adsense_management")],
    )
    # no applicable allocation, no channel-direct -> missing-net source
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.account_allocated_deduction_amount_usd is None


def test_basis_source_kind_mismatch_does_not_override_source_system():
    """A wrong basis_source_kind cannot make a cross-source allocation apply."""
    line = _alloc(source_system="adsense_management")
    object.__setattr__(line, "basis_source_kind", "YOUTUBE_CMS")  # frozen dataclass tweak
    summary = build_channel_net_revenue_summary(
        facts=[_fact(source_kind="YOUTUBE_CMS", net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[line],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"  # source_system gate still blocks


def test_non_net_applicable_allocation_never_reduces_net():
    """A non-net-applicable allocation must not reduce the net revenue."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(kind="UNRESOLVED_PAYMENT_GAP", net_applicable=False)],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_channel_direct_plus_account_allocated_sum():
    """Both contributions apply additively on the missing-net path."""
    from ums_smart_revenue.finance.deduction_components import DeductionComponent

    channel_direct = DeductionComponent(
        id="dc1", month=MONTH, component_kind="DEDUCTION", scope_kind="CHANNEL",
        scope_id=CH, amount_usd=Decimal("30.00"), amount_native=None,
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", source_id=None,
        source_key=None, source_report_id=None, raw_payload={}, component_key="cd1",
    )
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        deduction_components=[channel_direct],
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.net_revenue_usd == Decimal("870.000000")  # 1000 - 30 - 100
    assert summary.channel_direct_deduction_amount_usd == Decimal("30.00")
    assert summary.account_allocated_deduction_amount_usd == Decimal("100.000000")
    assert summary.deduction_amount_usd == Decimal("130.000000")


def test_safety_dedup_skips_duplicate_component_key():
    """An allocated line sharing a component_key with an applied channel-direct
    component is skipped (defensive; disjoint by construction).
    """
    from ums_smart_revenue.finance.deduction_components import DeductionComponent

    shared_key = "dup-key"
    channel_direct = DeductionComponent(
        id="dc1", month=MONTH, component_kind="DEDUCTION", scope_kind="CHANNEL",
        scope_id=CH, amount_usd=Decimal("30.00"), amount_native=None,
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", source_id=None,
        source_key=None, source_report_id=None, raw_payload={}, component_key=shared_key,
    )
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        deduction_components=[channel_direct],
        account_allocations=[_alloc(amount="100.000000", key=shared_key)],
    )
    # the duplicate-key allocation is skipped; only channel-direct applies
    assert summary.net_revenue_usd == Decimal("970.00")  # 1000 - 30
    assert summary.account_allocated_deduction_amount_usd == Decimal("0")


def test_month_unallocated_surface_global():
    """Month builder populates the unallocated surface from net-applicable issues."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        account_allocations=[_alloc(amount="100.000000")],
        unallocated_account_issues=[
            _issue(amount="40.000000", kind="DEDUCTION"),
            _issue(amount="5.000000", kind="UNRESOLVED_PAYMENT_GAP", code="UNSUPPORTED_SCOPE", key="u2"),
        ],
    )
    # only the net-applicable (DEDUCTION) issue is surfaced; reconciliation kind excluded
    assert summary.unallocated_account_deduction_total_usd == Decimal("40.000000")
    assert len(summary.unallocated_account_issues) == 1
    assert summary.unallocated_account_issues[0]["issue_code"] == "ACCOUNT_UNMAPPED_OR_UNVERIFIED"


def test_month_unallocated_surface_scoped_is_none():
    """When the caller withholds issues (scoped), both surface fields are None."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        account_allocations=[_alloc(amount="100.000000")],
        unallocated_account_issues=None,
    )
    assert summary.unallocated_account_deduction_total_usd is None
    assert summary.unallocated_account_issues is None


def test_default_no_allocations_is_unchanged_behavior():
    """Omitting the new params reproduces PR-B behavior exactly."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net="880.00", gross="1000.00")],
        manual_overrides=[],
    )
    assert summary.channels[0].net_revenue_usd == Decimal("880.00")
    assert summary.unallocated_account_deduction_total_usd is None
    assert summary.unallocated_account_issues is None
