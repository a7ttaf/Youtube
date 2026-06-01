from decimal import Decimal

from ums_smart_revenue.finance.allocation import AllocationLine, UnallocatedIssue
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    build_month_net_revenue_summary,
    filter_account_allocations_to_scope,
    resolve_applicable_channel_deductions,
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


def test_allocation_only_channel_is_included_without_keyerror():
    """A channel present only in allocations (no facts/overrides) is summarized
    via .get(channel_id, ()) instead of crashing or being dropped.
    """
    # "chZ" has an account allocation but no revenue fact or override; "chA"
    # has a fact only. Both must appear; the allocation-only channel must build.
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net="880.00", gross="1000.00", channel="chA")],
        manual_overrides=[],
        account_allocations=[_alloc(channel="chZ", amount="50.000000")],
    )
    ids = {channel.youtube_channel_id for channel in summary.channels}
    assert ids == {"chA", "chZ"}
    alloc_only = next(
        (c for c in summary.channels if c.youtube_channel_id == "chZ"), None
    )
    assert alloc_only is not None
    # No facts -> NO_FACTS status, built via .get(channel_id, ()) without KeyError.
    assert alloc_only.status == "NO_FACTS"


def test_filter_account_allocations_global_keeps_all_lines():
    """A global read (channel_ids is None) returns every allocation line."""
    lines = [_alloc(channel="chA"), _alloc(channel="chB", key="k2")]
    assert filter_account_allocations_to_scope(lines, None) == lines


def test_filter_account_allocations_scoped_drops_out_of_scope_lines():
    """A scoped read keeps only lines whose channel is inside the authorized set."""
    in_scope = _alloc(channel="chA", key="k1")
    out_of_scope = _alloc(channel="chB", key="k2")
    filtered = filter_account_allocations_to_scope(
        [in_scope, out_of_scope], {"chA"}
    )
    assert filtered == [in_scope]


def test_filter_account_allocations_empty_scope_drops_everything():
    """An empty authorized set is fail-closed: no allocation lines survive."""
    assert filter_account_allocations_to_scope([_alloc(channel="chA")], set()) == []


def _component(*, key="cd-1", kind="TAX", amount="30.00",
               source_system="adsense_management", channel=CH, month=MONTH):
    """Build a CHANNEL-scoped DeductionComponent for test scenarios."""
    return DeductionComponent(
        id=f"dc-{key}", month=month, component_kind=kind, scope_kind="CHANNEL",
        scope_id=channel, amount_usd=Decimal(amount), amount_native=None,
        currency_code="USD", source_system=source_system,
        source_table="deduction_components", source_id=None, source_key=None,
        source_report_id=None, raw_payload={}, component_key=key,
    )


def test_resolve_applicable_channel_deductions_filters_dedups_and_matches_totals():
    """The shared helper filters, dedups by component_key, and matches the builder totals."""
    components = [_component(key="cd-1", kind="TAX", amount="30.00")]
    allocations = [
        _alloc(key="acct-1", amount="100.000000"),               # applies
        _alloc(key="cd-1", amount="999.000000"),                 # dedup vs channel-direct key
        _alloc(channel="ch-other", key="acct-2", amount="500.000000"),  # wrong channel
    ]

    channel_direct, account_allocated = resolve_applicable_channel_deductions(
        deduction_components=components,
        account_allocations=allocations,
        month=MONTH,
        youtube_channel_id=CH,
        primary_source_kind="ADSENSE",
    )
    assert [c.component_key for c in channel_direct] == ["cd-1"]
    assert [line.component_key for line in account_allocated] == ["acct-1"]

    # No-drift: the helper's sums equal the builder's COMPONENT_DERIVED breakdown.
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],  # source_kind ADSENSE -> aligned
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        deduction_components=components,
        account_allocations=allocations,
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.channel_direct_deduction_amount_usd == sum(
        (c.amount_usd for c in channel_direct), Decimal("0")
    )
    assert summary.account_allocated_deduction_amount_usd == sum(
        (line.allocated_amount_usd for line in account_allocated), Decimal("0")
    )
