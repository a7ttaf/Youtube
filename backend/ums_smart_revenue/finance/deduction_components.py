from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import BankReconciliationEntry

USD = "USD"
COMPONENT_KINDS: tuple[str, ...] = (
    "TAX", "DEDUCTION", "TRANSFER_FEE", "FX_VARIANCE", "UNRESOLVED_PAYMENT_GAP",
)
SCOPE_KINDS: tuple[str, ...] = ("CHANNEL", "ACCOUNT", "PAYMENT")
_TAX_DEDUCTION_VALUE_KINDS: frozenset[str] = frozenset({"tax", "deduction"})
_SETTLED_VALUE_KIND = "settled"
_PAID_STATUS = "PAID"
_ADSENSE_SYSTEM = "adsense_management"
_GAP_SOURCE_SYSTEM = "adsense_payment_gap"


@dataclass(frozen=True)
class DeductionComponentInput:
    """One deduction-evidence component to upsert (no tenant/id/month yet)."""

    component_kind: str
    scope_kind: str
    scope_id: str
    amount_usd: Decimal
    amount_native: Decimal | None
    currency_code: str
    source_system: str
    source_table: str
    source_id: str | None
    source_key: str | None
    source_report_id: str | None
    raw_payload: dict[str, object]
    component_key: str


@dataclass(frozen=True)
"""
Module for mapping revenue source rows, bank reconciliation entries, and AdSense payment data
into DeductionComponentInput models for downstream API consumption.
"""

class DeductionComponent:
    """Persisted deduction-component read model."""

    id: str
    month: str
    component_kind: str
    scope_kind: str
    scope_id: str
    amount_usd: Decimal
    amount_native: Decimal | None
    currency_code: str
    source_system: str
    source_table: str
    source_id: str | None
    source_key: str | None
    source_report_id: str | None
    raw_payload: dict[str, object]
    component_key: str

    def to_api(self) -> dict[str, object]:
        """Convert the DeductionComponent instance into a dictionary compatible with the external API, excluding raw_payload for provenance."""
        # raw_payload is intentionally omitted (provenance only; see PR-B endpoint).
        return {
            "id": self.id,
            "month": self.month,
            "component_kind": self.component_kind,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "amount_usd": _decimal_to_api(self.amount_usd),
            "amount_native": (
                None if self.amount_native is None
                else _decimal_to_api(self.amount_native)
            ),
            "currency_code": self.currency_code,
            "source_system": self.source_system,
            "source_table": self.source_table,
            "source_id": self.source_id,
            "source_key": self.source_key,
            "source_report_id": self.source_report_id,
            "component_key": self.component_key,
        }


# ============================================================================
# Purpose: Map source-reported tax/deduction source rows into typed deduction
#   components. Channel-scoped when youtube_channel_id is present, else account.
# Database/ORM: None (pure over already-read GoogleRevenueSourceRowEntry rows).
# Standards: USD-only — non-USD rows are skipped and counted, never converted.
# Blast Radius: Finance read-model only. No writes, no auth.
# ============================================================================
def map_source_rows_to_components(
    rows: Iterable[GoogleRevenueSourceRowEntry],
) -> tuple[list[DeductionComponentInput], int]:
    """Transform GoogleRevenueSourceRowEntry records into DeductionComponentInput objects,
    counting and skipping any non-USD entries."""
    components: list[DeductionComponentInput] = []
    skipped_non_usd = 0
    for row in rows:
        if row.value_kind not in _TAX_DEDUCTION_VALUE_KINDS:
            continue
        if row.currency_code != USD:
            skipped_non_usd += 1
            continue
        if row.youtube_channel_id:
            scope_kind, scope_id = "CHANNEL", row.youtube_channel_id
        else:
            scope_kind, scope_id = "ACCOUNT", row.source_account_id
        components.append(
            DeductionComponentInput(
                component_kind=row.value_kind.upper(),
                scope_kind=scope_kind,
                scope_id=scope_id,
                amount_usd=row.amount_native,
                amount_native=row.amount_native,
                currency_code=row.currency_code,
                source_system=row.source_system,
                source_table="google_revenue_source_rows",
                source_id=row.id,
                source_key=row.source_row_key,
                source_report_id=row.source_report_id,
                raw_payload={
                    "value_kind": row.value_kind,
                    "metric_key": row.metric_key,
                    "source_row_key": row.source_row_key,
                },
                component_key=f"srcrow:{row.source_system}:{row.source_row_key}",
            )
        )
    return components, skipped_non_usd


# ============================================================================
# Purpose: Map bank reconciliation entries into PAYMENT-scoped components —
#   TRANSFER_FEE (deduction evidence) and signed FX_VARIANCE (variance, not a
#   blind deduction). Keyed by month + bank_reference (bank uniqueness key).
# Database/ORM: None (pure over BankReconciliationEntry).
# Standards: amounts are already USD; nothing is skipped for currency here.
# Blast Radius: Finance read-model only.
# ============================================================================
def map_bank_entries_to_components(
    entries: Iterable[BankReconciliationEntry],
    *,
    month: str,
) -> tuple[list[DeductionComponentInput], int]:
    """Convert BankReconciliationEntry records into PAYMENT-scoped DeductionComponentInput
    items for transfer fees and FX variance, keyed by month and bank reference."""
    components: list[DeductionComponentInput] = []
    for entry in entries:
        if entry.transfer_fee_usd > 0:
            components.append(
                _bank_component(
                    entry, month, "TRANSFER_FEE", entry.transfer_fee_usd,
                    "transfer_fee",
                )
            )
        if entry.fx_difference_usd != 0:
            components.append(
                _bank_component(
                    entry, month, "FX_VARIANCE", entry.fx_difference_usd,
                    "fx_variance",
                )
            )
    return components, 0


def _bank_component(
    entry: BankReconciliationEntry,
    month: str,
    kind: str,
    amount_usd: Decimal,
    key_suffix: str,
) -> DeductionComponentInput:
    """Create a DeductionComponentInput for a single bank reconciliation entry,
    scoped by payment and keyed by bank_reference and suffix."""
    return DeductionComponentInput(
        component_kind=kind,
        scope_kind="PAYMENT",
        scope_id=entry.bank_reference,
        amount_usd=amount_usd,
        amount_native=None,
        currency_code=USD,
        source_system="bank_reconciliation",
        source_table="bank_reconciliation_entries",
        source_id=entry.id,
        source_key=entry.bank_reference,
        source_report_id=entry.source_report_id,
        raw_payload={"bank_reference": entry.bank_reference, "kind": kind},
        component_key=f"bank:{month}:{entry.bank_reference}:{key_suffix}",
    )


# ============================================================================
# Purpose: Compute the account-level AdSense settled-earnings vs PAID-payment
#   gap as UNRESOLVED_PAYMENT_GAP evidence (signed). Reconciliation evidence
#   only — never labeled tax/withholding/fee. Emitted only when both a settled
#   earnings total and a PAID total exist for the account+month and they differ.
# Database/ORM: None (pure over source rows + payments).
# Standards: USD-only — non-USD settled rows / PAID payments are skipped+counted.
# Blast Radius: Finance read-model only.
# ============================================================================
def map_adsense_gap_to_components(
    *,
    month: str,
    source_rows: Iterable[GoogleRevenueSourceRowEntry],
    payments: Iterable[AdSensePaymentEntry],
) -> tuple[list[DeductionComponentInput], int]:
    """Calculate the difference between settled AdSense earnings and paid amounts per account,
    producing DeductionComponentInput entries for unresolved payment gaps and counting skipped non-USD entries."""
    skipped_non_usd = 0
    settled: dict[str, Decimal] = {}
    for row in source_rows:
        if row.source_system != _ADSENSE_SYSTEM or row.value_kind != _SETTLED_VALUE_KIND:
            continue
        if row.currency_code != USD:
            skipped_non_usd += 1
            continue
        settled[row.source_account_id] = (
            settled.get(row.source_account_id, Decimal("0")) + row.amount_native
        )
    paid: dict[str, Decimal] = {}
    for pay in payments:
        if pay.payment_status != _PAID_STATUS:
            continue
        if pay.payment_currency != USD:
            skipped_non_usd += 1
            continue
        paid[pay.source_account_id] = (
            paid.get(pay.source_account_id, Decimal("0")) + pay.payment_amount
        )
    components: list[DeductionComponentInput] = []
    for account in sorted(set(settled) & set(paid)):
        gap = settled[account] - paid[account]
"""
This module provides utilities for constructing deduction components and converting Decimal values
for API usage in the finance deduction components workflow.
"""
        if gap == 0:
            continue
        components.append(
            DeductionComponentInput(
                component_kind="UNRESOLVED_PAYMENT_GAP",
                scope_kind="ACCOUNT",
                scope_id=account,
                amount_usd=gap,
                amount_native=None,
                currency_code=USD,
                source_system=_GAP_SOURCE_SYSTEM,
                source_table="adsense_payment_gap",
                source_id=None,
                source_key=f"{account}:{month}",
                source_report_id=None,
                raw_payload={
                    "settled_earnings_usd": str(settled[account]),
                    "paid_usd": str(paid[account]),
                },
                component_key=f"adsense_gap:{account}:{month}",
            )
        )
    return components, skipped_non_usd


def _decimal_to_api(value: Decimal) -> str:
    """
    Convert a Decimal value to a string suitable for API responses,
    removing unnecessary trailing zeros and decimal points when not needed.
    """
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
