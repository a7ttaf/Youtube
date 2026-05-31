from decimal import Decimal

import pytest

from ums_smart_revenue.finance import allocation


def test_basis_source_kind_maps_known_systems():
    """source_system resolves to the matching raw-gross source kind."""
    assert allocation._basis_source_kind("adsense_management") == "ADSENSE"
    assert allocation._basis_source_kind("youtube_reporting") == "YOUTUBE_CMS"
    assert allocation._basis_source_kind("youtube_analytics") == "YOUTUBE_ANALYTICS"


def test_basis_source_kind_payment_gap_uses_adsense():
    """The AdSense payment-gap source maps to ADSENSE gross."""
    assert allocation._basis_source_kind("adsense_payment_gap") == "ADSENSE"


def test_basis_source_kind_unknown_returns_none():
    """An unresolvable source_system returns None (caller fails closed)."""
    assert allocation._basis_source_kind("bank_reconciliation") is None


def test_proportional_allocation_conserves_amount_exactly():
    """Largest-remainder split sums back to the input amount to 1e-6."""
    weights = [("a", Decimal("2")), ("b", Decimal("1"))]
    result = allocation._proportional_allocation(Decimal("9.000000"), weights)
    assert result["a"] == Decimal("6.000000")
    assert result["b"] == Decimal("3.000000")
    assert sum(result.values()) == Decimal("9.000000")


def test_proportional_allocation_residual_is_deterministic():
    """1/3 split: residual micro-unit goes to the deterministic tiebreak."""
    weights = [("c3", Decimal("1")), ("c1", Decimal("1")), ("c2", Decimal("1"))]
    result = allocation._proportional_allocation(Decimal("1.000000"), weights)
    assert sum(result.values()) == Decimal("1.000000")
    # equal remainders -> channel_id ascending wins the leftover unit
    assert result["c1"] == Decimal("0.333334")
    assert result["c2"] == Decimal("0.333333")
    assert result["c3"] == Decimal("0.333333")


def _component(
    *,
    scope_id="pub-1",
    component_kind="DEDUCTION",
    amount="100.00",
    source_system="adsense_management",
    scope_kind="ACCOUNT",
    key="k1",
):
    """Build a DeductionComponent for the allocator (only used fields matter)."""
    return DeductionComponent(
        id="id-" + key,
        month="2026-04",
        component_kind=component_kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="google_revenue_source_rows",
        source_id=None,
        source_key=None,
        source_report_id=None,
        raw_payload={},
        component_key=key,
    )


from ums_smart_revenue.finance.allocation import build_account_allocation  # noqa: E402
from ums_smart_revenue.finance.deduction_components import DeductionComponent  # noqa: E402


def test_allocates_by_source_aligned_gross_and_conserves():
    """A $100 ADSENSE deduction splits 70/30 by ADSENSE gross; sum == 100."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="100.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("700"), ("chB", "ADSENSE"): Decimal("300")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("70.000000"), "chB": Decimal("30.000000")}
    assert all(ln.net_applicable for ln in result.lines)
    assert result.summary.allocated_total_usd == Decimal("100.000000")
    assert result.summary.unallocated_total_usd == Decimal("0")


def test_single_channel_gets_full_amount():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="42.50")],
        verified_channels={"pub-1": ["only"]},
        gross_basis={("only", "ADSENSE"): Decimal("5")},
    )
    assert result.lines[0].allocated_amount_usd == Decimal("42.500000")


def test_source_alignment_ignores_other_source_kind_gross():
    """An adsense_management component must not weight by YOUTUBE_CMS gross."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", source_system="adsense_management")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "YOUTUBE_CMS"): Decimal("999")},  # wrong source kind
    )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_MISSING"


def test_net_applicable_buckets_split_correctly():
    """TAX/DEDUCTION are net-applicable; UNRESOLVED_PAYMENT_GAP is reconciliation."""
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(component_kind="DEDUCTION", amount="10.00", key="d"),
            _component(
                component_kind="UNRESOLVED_PAYMENT_GAP", amount="4.00",
                source_system="adsense_payment_gap", key="g",
            ),
        ],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},
    )
    assert result.summary.net_applicable_total_usd == Decimal("10.000000")
    assert result.summary.reconciliation_total_usd == Decimal("4.000000")
    assert (
        result.summary.net_applicable_total_usd
        + result.summary.reconciliation_total_usd
        == result.summary.allocated_total_usd
    )


def test_zero_amount_component_allocated_with_no_lines():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="0.00")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},
    )
    assert result.lines == ()
    assert result.summary.allocated_component_count == 1
    assert result.summary.unallocated_component_count == 0


def test_unmapped_account_is_unallocated():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", scope_id="pub-x")],
        verified_channels={},
        gross_basis={},
    )
    assert result.unallocated[0].issue_code == "ACCOUNT_UNMAPPED_OR_UNVERIFIED"
    assert result.summary.unallocated_total_usd == Decimal("10.000000")


def test_incomplete_basis_fails_closed():
    """One verified channel lacks source-aligned gross -> whole component blocked."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},  # chB absent
    )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_INCOMPLETE"


def test_present_zero_basis_is_valid_when_total_positive():
    """A present-zero channel is valid (gets 0); only absent triggers incomplete."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100"), ("chB", "ADSENSE"): Decimal("0")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("10.000000"), "chB": Decimal("0.000000")}


def test_zero_total_basis_is_unallocated():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("0")},
    )
    assert result.unallocated[0].issue_code == "ZERO_GROSS_BASIS"


def test_unsupported_scope_is_guarded():
    """A non-ACCOUNT component handed to the service is surfaced, not dropped."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(scope_kind="PAYMENT", scope_id="BANK-1", amount="5.00")],
        verified_channels={},
        gross_basis={},
    )
    assert result.unallocated[0].issue_code == "UNSUPPORTED_SCOPE"
    assert result.summary.unallocated_total_usd == Decimal("5.000000")


def test_channel_in_multiple_accounts_emits_informational_note():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", scope_id="pub-1", key="a")],
        verified_channels={"pub-1": ["shared"], "pub-2": ["shared"]},
        gross_basis={("shared", "ADSENSE"): Decimal("100")},
    )
    assert any(n.note_code == "CHANNEL_IN_MULTIPLE_ACCOUNTS" for n in result.notes)
    # allocation still proceeds for pub-1
    assert result.lines[0].allocated_amount_usd == Decimal("10.000000")


def test_aggregate_conservation_across_components():
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(amount="100.00", scope_id="pub-1", key="a"),
            _component(amount="7.00", scope_id="pub-2", key="b"),
            _component(amount="3.00", scope_id="pub-x", key="c"),  # unmapped
        ],
        verified_channels={"pub-1": ["chA", "chB"], "pub-2": ["chC"]},
        gross_basis={
            ("chA", "ADSENSE"): Decimal("1"),
            ("chB", "ADSENSE"): Decimal("1"),
            ("chC", "ADSENSE"): Decimal("9"),
        },
    )
    total_in = Decimal("110.00")
    assert (
        result.summary.allocated_total_usd + result.summary.unallocated_total_usd
        == total_in
    )


def test_proportional_allocation_rejects_non_positive_basis():
    with pytest.raises(allocation.AllocationValidationError):
        allocation._proportional_allocation(Decimal("10.000000"), [])
    with pytest.raises(allocation.AllocationValidationError):
        allocation._proportional_allocation(Decimal("10.000000"), [("a", Decimal("0"))])


def test_proportional_allocation_negative_residual_conserves():
    result = allocation._proportional_allocation(
        Decimal("-1.000000"),
        [("c1", Decimal("1")), ("c2", Decimal("1")), ("c3", Decimal("1"))],
    )
    assert sum(result.values()) == Decimal("-1.000000")
    assert result == {
        "c1": Decimal("-0.333333"),
        "c2": Decimal("-0.333333"),
        "c3": Decimal("-0.333334"),
    }


def test_negative_total_basis_is_unallocated():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="5.00")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("-5")},
    )
    assert result.unallocated[0].issue_code == "ZERO_GROSS_BASIS"


def test_allocates_negative_amount_preserving_sign_and_conservation():
    """A negative UNRESOLVED_PAYMENT_GAP (settled - paid) splits with sign + exact sum."""
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(
                component_kind="UNRESOLVED_PAYMENT_GAP", amount="-9.00",
                source_system="adsense_payment_gap", key="neg",
            )
        ],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("2"), ("chB", "ADSENSE"): Decimal("1")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("-6.000000"), "chB": Decimal("-3.000000")}
    assert sum(by_channel.values()) == Decimal("-9.000000")
    assert all(not ln.net_applicable for ln in result.lines)  # reconciliation bucket
    assert result.summary.reconciliation_total_usd == Decimal("-9.000000")
