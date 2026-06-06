"""Unit tests for the pure manual account-allocation builder."""
from decimal import Decimal

import pytest

from ums_smart_revenue.finance.allocation import AllocationValidationError
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.manual_allocation import (
    MANUAL_ALLOCATION_METHOD,
    ManualAllocationInput,
    build_manual_account_allocation,
)

MONTH = "2026-03"


def _component(
    *,
    component_key: str,
    scope_id: str = "pub-1",
    amount_usd: str = "100.00",
    component_kind: str = "DEDUCTION",
    scope_kind: str = "ACCOUNT",
    source_system: str = "adsense_management",
) -> DeductionComponent:
    """Build a minimal DeductionComponent for manual-allocation tests."""
    return DeductionComponent(
        id=f"id-{component_key}",
        month=MONTH,
        component_kind=component_kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount_usd),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="google_revenue_source_rows",
        source_id=None,
        source_key=None,
        source_report_id=None,
        raw_payload={},
        component_key=component_key,
    )


def _line(component_key: str, channel: str, amount: str) -> ManualAllocationInput:
    """Shorthand ManualAllocationInput constructor."""
    return ManualAllocationInput(
        component_key=component_key,
        youtube_channel_id=channel,
        amount_usd=Decimal(amount),
    )


def test_happy_path_two_components_two_channels():
    """Two components each split across two channels allocate exactly."""
    components = [
        _component(component_key="ad-1", amount_usd="100.00"),
        _component(component_key="ad-2", scope_id="pub-2", amount_usd="60.00"),
    ]
    verified = {"pub-1": ("chA", "chB"), "pub-2": ("chC", "chD")}
    lines = [
        _line("ad-1", "chA", "70.00"),
        _line("ad-1", "chB", "30.00"),
        _line("ad-2", "chC", "20.00"),
        _line("ad-2", "chD", "40.00"),
    ]
    result = build_manual_account_allocation(
        month=MONTH, components=components,
        verified_channels=verified, manual_lines=lines,
    )
    assert result.allocation_method == MANUAL_ALLOCATION_METHOD
    assert result.unallocated == ()
    assert result.notes == ()
    assert len(result.lines) == 4
    by_key = {(ln.component_key, ln.youtube_channel_id): ln for ln in result.lines}
    assert by_key[("ad-1", "chA")].allocated_amount_usd == Decimal("70.00")
    assert by_key[("ad-1", "chA")].basis_source_kind == "MANUAL"
    assert by_key[("ad-1", "chA")].basis_share == Decimal("0.700000")
    assert by_key[("ad-1", "chA")].net_applicable is True
    assert result.summary.component_count == 2
    assert result.summary.allocated_component_count == 2
    assert result.summary.allocated_total_usd == Decimal("160.00")


def test_line_order_is_deterministic():
    """Lines sort by (component_key, youtube_channel_id) regardless of input order."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA", "chB")}
    lines = [_line("ad-1", "chB", "30.00"), _line("ad-1", "chA", "70.00")]
    result = build_manual_account_allocation(
        month=MONTH, components=components,
        verified_channels=verified, manual_lines=lines,
    )
    order = [(ln.component_key, ln.youtube_channel_id) for ln in result.lines]
    assert order == [("ad-1", "chA"), ("ad-1", "chB")]


def test_unknown_component_key_rejected():
    """A line referencing an unknown component_key fails closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA",)}
    lines = [_line("ad-1", "chA", "100.00"), _line("ghost", "chA", "0.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "ghost" in str(exc.value)


def test_channel_not_verified_rejected():
    """A line whose channel is not verified for the account fails closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA",)}
    lines = [_line("ad-1", "chZ", "100.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "chZ" in str(exc.value)


def test_duplicate_pair_rejected():
    """A duplicate (component_key, channel) pair fails closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA",)}
    lines = [_line("ad-1", "chA", "60.00"), _line("ad-1", "chA", "40.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "ad-1" in str(exc.value)
    assert "chA" in str(exc.value)


def test_negative_amount_rejected():
    """A negative line amount fails closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA", "chB")}
    lines = [_line("ad-1", "chA", "120.00"), _line("ad-1", "chB", "-20.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "chB" in str(exc.value)


def test_over_precision_amount_rejected():
    """A line amount with more than 6 decimal places fails closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA",)}
    lines = [_line("ad-1", "chA", "100.0000001")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "chA" in str(exc.value)


def test_sum_mismatch_rejected():
    """Lines whose amounts do not sum exactly to the component amount fail closed."""
    components = [_component(component_key="ad-1", amount_usd="100.00")]
    verified = {"pub-1": ("chA", "chB")}
    lines = [_line("ad-1", "chA", "70.00"), _line("ad-1", "chB", "20.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    message = str(exc.value)
    assert "ad-1" in message
    assert "100" in message
    assert "90" in message


def test_uncovered_component_rejected():
    """An ACCOUNT component with no lines fails closed and is named."""
    components = [
        _component(component_key="ad-1", amount_usd="100.00"),
        _component(component_key="ad-2", scope_id="pub-2", amount_usd="50.00"),
    ]
    verified = {"pub-1": ("chA",), "pub-2": ("chC",)}
    lines = [_line("ad-1", "chA", "100.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "ad-2" in str(exc.value)


def test_non_account_component_rejects_whole_request():
    """Any non-ACCOUNT component rejects the entire manual request."""
    components = [
        _component(component_key="ad-1", amount_usd="100.00"),
        _component(
            component_key="ch-1", scope_kind="CHANNEL", scope_id="chA",
            amount_usd="10.00",
        ),
    ]
    verified = {"pub-1": ("chA",)}
    lines = [_line("ad-1", "chA", "100.00")]
    with pytest.raises(AllocationValidationError) as exc:
        build_manual_account_allocation(
            month=MONTH, components=components,
            verified_channels=verified, manual_lines=lines,
        )
    assert "ch-1" in str(exc.value)


def test_reconciliation_component_kind_not_net_applicable():
    """A non-net-applicable kind (e.g. FX_VARIANCE) sets net_applicable False."""
    components = [
        _component(
            component_key="fx-1", amount_usd="100.00", component_kind="FX_VARIANCE",
        ),
    ]
    verified = {"pub-1": ("chA",)}
    lines = [_line("fx-1", "chA", "100.00")]
    result = build_manual_account_allocation(
        month=MONTH, components=components,
        verified_channels=verified, manual_lines=lines,
    )
    assert result.lines[0].net_applicable is False
    assert result.summary.net_applicable_total_usd == Decimal("0")
    assert result.summary.reconciliation_total_usd == Decimal("100.00")
