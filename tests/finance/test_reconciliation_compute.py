from decimal import Decimal

from ums_smart_revenue.finance.reconciliation_workflow import (
    DEFAULT_US_WITHHOLDING_RATE,
    NullUsViewShareProvider,
    compute_month_reconciliation,
)

D = Decimal


def _gross(**kw):
    return {k: D(v) for k, v in kw.items()}


def test_single_channel_full_passthrough_no_data():
    # No adsense/bank/us-view => only gross survives (hops 2/3 = 0, tax = 0).
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=None,
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.youtube_channel_id == "c1"
    assert line.us_tax_usd == D("0.000000")
    assert line.yt_adsense_fee_usd == D("0.000000")
    assert line.adsense_bank_fee_usd == D("0.000000")
    assert line.fx_variance_usd == D("0.000000")
    assert line.net_received_usd == D("100.000000")
    assert any(w["code"] == "MISSING_ADSENSE_TOTAL" for w in res.warnings)


def test_tax_uses_us_view_share_times_rate():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": D("0.5")},
        adsense_received_usd=None,
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    # 0.5 * 100 * 0.30 = 15
    assert res.channels[0].us_tax_usd == D("15.000000")
    assert res.channels[0].net_received_usd == D("85.000000")


def test_yt_adsense_fee_is_residual_attributed_by_gross():
    # gross 100 (c1=60, c2=40), no tax; adsense received 80 => fee 20 total
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="60", c2="40"),
        us_view_shares={"c1": None, "c2": None},
        adsense_received_usd=D("80"),
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    by = {c.youtube_channel_id: c for c in res.channels}
    assert by["c1"].yt_adsense_fee_usd == D("12.000000")  # 20 * 60/100
    assert by["c2"].yt_adsense_fee_usd == D("8.000000")   # 20 * 40/100
    # fee total attributed exactly
    total_fee = by["c1"].yt_adsense_fee_usd + by["c2"].yt_adsense_fee_usd
    assert total_fee == D("20.000000")


def test_adsense_bank_split_fee_and_fx():
    # adsense 80, bank 60 => delta 20; fx_total 5 => fee 15
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("5"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.fx_variance_usd == D("5.000000")
    assert line.adsense_bank_fee_usd == D("15.000000")  # 20 - 5


def test_zero_gross_with_bank_evidence_warns_and_suppresses_hop_three_totals():
    """Nonzero bank/FX evidence cannot be published against a zero gross basis."""
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="0"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("5"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.adsense_bank_fee_usd == D("0.000000")
    assert line.fx_variance_usd == D("0.000000")
    assert res.adsense_bank_fee_total_usd == D("0.000000")
    assert res.fx_total_usd == D("0.000000")
    assert any(
        w["code"] == "ZERO_GROSS_RECONCILIATION_BASIS" for w in res.warnings
    )


def test_negative_fx_variance_preserves_sign_and_adjusts_fee():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("-5"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.fx_variance_usd == D("-5.000000")
    assert line.adsense_bank_fee_usd == D("25.000000")
    assert line.net_received_usd == D("60.000000")
    assert res.fx_total_usd == D("-5.000000")


def test_net_sum_reconciles_to_bank_when_data_present():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="60", c2="40"),
        us_view_shares={"c1": D("0.1"), "c2": D("0.2")},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("3"),
        withholding_rate=D("0.30"),
    )
    total_net = sum((c.net_received_usd for c in res.channels), D("0"))
    assert total_net == D("60.000000")  # equals bank received exactly


def test_rounding_remainder_lands_on_largest_gross():
    # gross split that forces a rounding drift on the fee attribution
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="1", c2="1", c3="1"),
        us_view_shares={"c1": None, "c2": None, "c3": None},
        adsense_received_usd=D("2"),  # fee total = 1 over 3 channels
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    total_fee = sum((c.yt_adsense_fee_usd for c in res.channels), D("0"))
    assert total_fee == D("1.000000")  # remainder reconciled, no drift


def test_anomaly_when_adsense_exceeds_estimate_clamps_and_warns():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("120"),  # more than estimate
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    assert res.channels[0].yt_adsense_fee_usd == D("0.000000")
    assert any(w["code"] == "RECONCILIATION_ANOMALY" for w in res.warnings)


def test_null_provider_returns_none():
    assert NullUsViewShareProvider().us_view_share("2026-03", "c1") is None


def test_default_rate_is_decimal():
    assert isinstance(DEFAULT_US_WITHHOLDING_RATE, Decimal)
