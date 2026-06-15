"""Channel-direct deduction consumption in net-revenue (only when source net missing)."""

from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH = "2026-04"
CHANNEL = "chan-1"


def _mod():
    """Import and return the net_revenue module for builder access."""
    return import_module("ums_smart_revenue.finance.net_revenue")


def fact(*, source_kind="ADSENSE", gross="1000.00", net=None):
    """Build a RevenueFactEntry for the fixed test channel/month."""
    return RevenueFactEntry(
        id=f"{source_kind}-{CHANNEL}",
        month=MONTH,
        youtube_channel_id=CHANNEL,
        source_kind=source_kind,
        source_report_id=f"{source_kind}-{MONTH}",
        gross_revenue_usd=Decimal(gross),
        net_revenue_usd=None if net is None else Decimal(net),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("1"),
        imported_by=None,
    )


def component(
    *,
    kind="DEDUCTION",
    scope_kind="CHANNEL",
    scope_id=CHANNEL,
    amount="120.00",
    source_system="adsense_management",
    month=MONTH,
):
    """Build a persisted DeductionComponent read-model row for tests."""
    return DeductionComponent(
        id=f"dc-{kind}-{scope_id}-{amount}",
        month=month,
        component_kind=kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="google_revenue_source_rows",
        source_id=None,
        source_key=f"k-{kind}-{amount}",
        source_report_id=None,
        raw_payload={"k": "v"},
        component_key=f"srcrow:{source_system}:{kind}-{amount}",
    )


def _channel(*, facts, components=()):
    """Call build_channel_net_revenue_summary for the fixed test channel and month."""
    return _mod().build_channel_net_revenue_summary(
        facts=facts,
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CHANNEL,
        deduction_components=components,
    )


def test_net_present_path_unchanged_components_ignored_for_net():
    """When a source net is present, components are ignored and
    the official net path is used unchanged."""
    # Source net present -> official net path untouched; components do NOT subtract.
    summary = _channel(
        facts=[fact(net="900.00")],
        components=[component(amount="120.00")],
    )
    assert summary.status == "CALCULATED"
    assert summary.net_revenue_usd == Decimal("900.00")
    assert summary.confidence == "B_RECONCILED"


def test_missing_net_with_channel_components_is_component_derived():
    """Missing source net + applicable CHANNEL components yields
    COMPONENT_DERIVED status and derived net."""
    summary = _channel(
        facts=[fact(net=None, gross="1000.00")],
        components=[
            component(kind="DEDUCTION", amount="120.00"),
            component(kind="TAX", amount="30.00", source_system="adsense_management"),
        ],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.confidence == "D_ESTIMATED"
    assert summary.net_revenue_usd == Decimal("850.00")  # 1000 - (120 + 30)
    assert summary.deduction_amount_usd == Decimal("150.00")


def test_missing_net_without_applicable_components_stays_missing():
    """Missing source net with no applicable components produces
    NET_REVENUE_SOURCE_MISSING status."""
    summary = _channel(facts=[fact(net=None)], components=[])
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None
    assert summary.confidence == "E_MISSING"


def test_cross_source_components_excluded_from_derived_net():
    """A component from a different source_system than the primary
    fact is excluded from net derivation."""
    # Primary is ADSENSE; a youtube_reporting (YOUTUBE_CMS) component must NOT apply.
    summary = _channel(
        facts=[fact(source_kind="ADSENSE", net=None)],
        components=[component(amount="120.00", source_system="youtube_reporting")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None


def test_allocation_source_components_apply_to_allocation_fact():
    """Reconciliation ALLOCATION facts use reconciliation TAX components for net derivation."""
    summary = _channel(
        facts=[fact(source_kind="ALLOCATION", gross="42.00", net=None)],
        components=[component(kind="TAX", amount="6.30", source_system="reconciliation")],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.net_revenue_usd == Decimal("35.70")
    assert summary.deduction_amount_usd == Decimal("6.30")


def test_account_scoped_components_never_affect_net():
    """ACCOUNT-scoped components are never applied to channel net derivation."""
    summary = _channel(
        facts=[fact(net=None)],
        components=[component(scope_kind="ACCOUNT", scope_id="pub-1", amount="120.00")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_payment_and_fee_fx_gap_components_never_affect_net():
    """TRANSFER_FEE, FX_VARIANCE, and UNRESOLVED_PAYMENT_GAP
    component kinds never reduce channel net."""
    summary = _channel(
        facts=[fact(net=None)],
        components=[
            component(kind="TRANSFER_FEE", scope_kind="PAYMENT", scope_id="BANK-1", amount="5.00"),
            component(kind="FX_VARIANCE", scope_kind="PAYMENT", scope_id="BANK-1", amount="-2.00"),
            component(
                kind="UNRESOLVED_PAYMENT_GAP",
                scope_kind="ACCOUNT",
                scope_id="pub-1",
                amount="70.00",
            ),
        ],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_other_channel_components_excluded():
    """A CHANNEL component for a different scope_id is excluded from net derivation."""
    summary = _channel(
        facts=[fact(net=None)],
        components=[component(scope_id="other-chan", amount="120.00")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_month_summary_includes_component_derived_channel_in_totals():
    """build_month_net_revenue_summary counts COMPONENT_DERIVED
    channels in totals and not in missing count."""
    mod = _mod()
    summary = mod.build_month_net_revenue_summary(
        month=MONTH,
        facts=[fact(net=None, gross="1000.00")],
        manual_overrides=[],
        deduction_components=[component(kind="DEDUCTION", amount="120.00")],
    )
    channel = summary.channels[0]
    assert channel.status == "COMPONENT_DERIVED"
    assert summary.total_net_revenue_usd == Decimal("880.00")  # 1000 - 120
    assert summary.missing_net_source_count == 0


def test_over_deduction_yields_negative_net_without_clamp():
    """When total components exceed gross, the derived net is negative and not clamped to zero."""
    # Evidence-derived net is NOT clamped: Sigma(components) > adjusted_gross
    # produces a negative Decimal net. Locks this in so a future well-meaning
    # clamp cannot silently change finance output.
    summary = _channel(
        facts=[fact(net=None, gross="100.00")],
        components=[component(kind="DEDUCTION", amount="150.00")],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.net_revenue_usd == Decimal("-50.00")  # 100 - 150
    assert summary.deduction_amount_usd == Decimal("150.00")


def test_channel_builder_excludes_other_month_components():
    """A component with a different month than the fact is excluded from channel net derivation."""
    # A component for a different month must NOT derive net for this month's fact.
    summary = _channel(
        facts=[fact(net=None, gross="1000.00")],
        components=[component(kind="DEDUCTION", amount="120.00", month="2026-03")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None


def test_month_builder_excludes_other_month_components():
    """build_month_net_revenue_summary ignores components from a different month than the target."""
    summary = _mod().build_month_net_revenue_summary(
        month=MONTH,
        facts=[fact(net=None, gross="1000.00")],
        manual_overrides=[],
        deduction_components=[component(kind="DEDUCTION", amount="90.00", month="2026-03")],
    )
    channel = summary.channels[0]
    assert channel.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.missing_net_source_count == 1
