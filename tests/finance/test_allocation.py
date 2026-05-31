from decimal import Decimal

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
