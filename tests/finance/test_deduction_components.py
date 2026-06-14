"""Tests for the pure deduction-component mappers."""

from datetime import date, datetime
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import BankReconciliationEntry

MONTH = "2026-04"


def _mod():
    """Dynamically import the deduction_components module for testing."""
    return import_module("ums_smart_revenue.finance.deduction_components")


def source_row(
    *,
    value_kind,
    amount,
    currency="USD",
    account="pub-1",
    channel=None,
    system="adsense_management",
    key=None,
):
    """Build a GoogleRevenueSourceRowEntry for tests."""
    return GoogleRevenueSourceRowEntry(
        id=f"row-{key or value_kind}-{amount}",
        tenant_id="t",
        source_system=system,
        source_row_key=(key or f"{value_kind}-{amount}").ljust(64, "0")[:64],
        source_account_id=account,
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type="report",
        report_month=MONTH,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key="m",
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r1",
        raw_file_id=None,
        raw_payload={"k": "v"},
        imported_by=None,
        ingested_at=datetime(2026, 5, 1, 12, 0, 0),
    )


def bank_entry(*, reference, fee="0.00", fx="0.00"):
    """Build a BankReconciliationEntry for tests."""
    return BankReconciliationEntry(
        id=f"bank-{reference}",
        month=MONTH,
        bank_reference=reference,
        bank_received_date=date(2026, 4, 20),
        bank_received_amount=Decimal("1000.00"),
        bank_received_currency="USD",
        bank_received_amount_usd=Decimal("1000.00"),
        transfer_fee_usd=Decimal(fee),
        fx_difference_usd=Decimal(fx),
        notes=None,
        source_report_id=None,
        recorded_by="user",
    )


def payment(*, account, amount, status="PAID", currency="USD", name="p"):
    """Build an AdSensePaymentEntry for tests."""
    return AdSensePaymentEntry(
        id=f"pay-{account}-{name}",
        source_account_id=account,
        month=MONTH,
        payment_name=name,
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={},
        source_report_id=None,
        imported_by=None,
    )


# ---- value_kind tax/deduction consumer ----


def test_source_rows_channel_scoped_tax_becomes_channel_component():
    """Verify channel-scoped tax rows are mapped to channel components."""
    components, skipped = _mod().map_source_rows_to_components(
        [source_row(value_kind="tax", amount="12.00", channel="chan-1")]
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == ("TAX", "CHANNEL", "chan-1")
    assert c.amount_usd == Decimal("12.00")
    assert c.source_system == "adsense_management"
    assert c.component_key == f"srcrow:adsense_management:{components[0].source_key}"


def test_source_rows_account_scoped_deduction_when_no_channel():
    """Ensure deduction without a channel uses account-scoped deduction component."""
    components, _ = _mod().map_source_rows_to_components(
        [source_row(value_kind="deduction", amount="5.00", channel=None, account="pub-9")]
    )
    assert (components[0].component_kind, components[0].scope_kind, components[0].scope_id) == (
        "DEDUCTION",
        "ACCOUNT",
        "pub-9",
    )


def test_source_rows_ignores_non_tax_deduction_value_kinds():
    """Check non-tax and non-deduction kinds are ignored with no skipped count."""
    components, skipped = _mod().map_source_rows_to_components(
        [
            source_row(value_kind="settled", amount="100.00"),
            source_row(value_kind="estimated", amount="50.00"),
        ]
    )
    assert components == []
    assert skipped == 0


def test_source_rows_skips_non_usd_and_counts_it():
    """Validate non-USD rows are skipped and counted."""
    components, skipped = _mod().map_source_rows_to_components(
        [source_row(value_kind="tax", amount="9.00", currency="EUR")]
    )
    assert components == []
    assert skipped == 1


# ---- bank fee / FX ----


def test_bank_transfer_fee_is_payment_scoped_deduction():
    """Test that bank transfer fees become payment-scoped deductions."""
    components, skipped = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-1", fee="3.50", fx="0.00")], month=MONTH
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == ("TRANSFER_FEE", "PAYMENT", "BANK-1")
    assert c.amount_usd == Decimal("3.50")
    assert c.component_key == f"bank:{MONTH}:BANK-1:transfer_fee"


def test_bank_fx_variance_is_signed_and_not_a_fee():
    """Ensure FX variance is represented as a signed component, not a fee."""
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-2", fee="0.00", fx="-7.25")], month=MONTH
    )
    assert len(components) == 1
    c = components[0]
    assert c.component_kind == "FX_VARIANCE"
    assert c.amount_usd == Decimal("-7.25")
    assert c.component_key == f"bank:{MONTH}:BANK-2:fx_variance"


def test_bank_zero_fee_and_zero_fx_produce_nothing():
    """Verify entries with zero fee and zero FX produce no components."""
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-3", fee="0.00", fx="0.00")], month=MONTH
    )
    assert components == []


def test_bank_entry_with_both_fee_and_fx_emits_two_components():
    """Check entries with both fee and FX create two separate components."""
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-4", fee="2.00", fx="1.00")], month=MONTH
    )
    assert {c.component_kind for c in components} == {"TRANSFER_FEE", "FX_VARIANCE"}


# ---- AdSense earnings -> payment gap ----


def test_gap_emitted_only_when_settled_and_paid_both_present_and_differ():
    """Validate that a gap is emitted only when settled and paid differ."""
    components, skipped = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == (
        "UNRESOLVED_PAYMENT_GAP",
        "ACCOUNT",
        "pub-1",
    )
    assert c.amount_usd == Decimal("70.00")  # settled 1000 - paid 930
    assert c.source_system == "adsense_payment_gap"
    assert c.component_key == f"adsense_gap:pub-1:{MONTH}"


def test_gap_signed_when_paid_exceeds_settled():
    """Ensure the gap amount is negative when paid exceeds settled."""
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="900.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="950.00")],
    )
    assert components[0].amount_usd == Decimal("-50.00")


def test_gap_skipped_when_no_settled_rows():
    """Check no gap is emitted when there are no settled source rows."""
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="estimated", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []


def test_gap_skipped_when_no_paid_payment():
    """Verify no gap is emitted when payment status is not PAID."""
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00", status="PENDING")],
    )
    assert components == []


def test_gap_skips_non_usd_settled_or_paid_and_counts_it():
    """Ensure non-USD settled or paid entries are skipped and counted."""
    components, skipped = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[
            source_row(value_kind="settled", amount="1000.00", account="pub-1", currency="EUR")
        ],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []
    assert skipped == 1


def test_gap_zero_difference_produces_nothing():
    """Check that zero difference between settled and paid produces no components."""
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="930.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []


def test_to_api_excludes_raw_payload_and_serializes_decimal():
    """Verify to_api omits raw_payload and formats decimals without trailing zeros."""
    # to_api lives on the persisted read model (DeductionComponent). It omits
    # raw_payload and serializes amounts with the repo's trailing-zero-trimming
    # convention ("3.50" -> "3.5"), matching payment_status._decimal_to_api.
    component = _mod().DeductionComponent(
        id="row-1",
        month=MONTH,
        component_kind="TRANSFER_FEE",
        scope_kind="PAYMENT",
        scope_id="BANK-5",
        amount_usd=Decimal("3.50"),
        amount_native=None,
        currency_code="USD",
        source_system="bank_reconciliation",
        source_table="bank_reconciliation_entries",
        source_id=None,
        source_key="BANK-5",
        source_report_id=None,
        raw_payload={"k": "v"},
        component_key="bank:2026-04:BANK-5:transfer_fee",
    )
    api = component.to_api()
    assert "raw_payload" not in api
    assert api["amount_usd"] == "3.5"
    assert api["component_kind"] == "TRANSFER_FEE"
