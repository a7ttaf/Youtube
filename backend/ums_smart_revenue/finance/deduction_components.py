"""Map source-of-truth finance evidence into deduction-component records."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
    GoogleRevenueSourceRowEntry,
    SourceRowProjectionDisposition,
)
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import BankReconciliationEntry
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api

USD = "USD"
COMPONENT_KINDS: tuple[str, ...] = (
    "TAX",
    "DEDUCTION",
    "TRANSFER_FEE",
    "FX_VARIANCE",
    "UNRESOLVED_PAYMENT_GAP",
)
SCOPE_KINDS: tuple[str, ...] = ("CHANNEL", "ACCOUNT", "PAYMENT")
_TAX_DEDUCTION_VALUE_KINDS: frozenset[str] = frozenset({"tax", "deduction"})
_SETTLED_VALUE_KIND = "settled"
_PAID_STATUS = "PAID"
_ADSENSE_SYSTEM = "adsense_management"
_GAP_SOURCE_SYSTEM = "adsense_payment_gap"


# ============================================================================
# Purpose: Independently fence Analytics evidence before the direct deduction
#          consumer interprets tax/deduction value kinds.
# Database/ORM: None (pure over persisted source-row provenance).
# Standards: Exact report/disposition/dimension contract; malformed or unknown
#            Analytics provenance fails closed and cannot reduce net revenue.
# Blast Radius: Deduction ingestion and official net-revenue calculations.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> Emits the projecting provenance contract.
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> Calls
#     map_source_rows_to_components over month-wide source rows.
# ============================================================================
def _is_projecting_deduction_source_row(row: GoogleRevenueSourceRowEntry) -> bool:
    """Return whether a direct source-row deduction consumer may read the row."""
    if row.source_system != "youtube_analytics":
        return True
    dimensions = row.raw_payload.get("dimensions")
    disposition = row.raw_payload.get("projection_disposition")
    return (
        row.report_type == YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE
        and isinstance(dimensions, Mapping)
        and "country" not in dimensions
        and disposition
        in {
            None,
            SourceRowProjectionDisposition.PROJECTING.value,
        }
    )


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
        """Return the API payload, excluding raw provenance details."""
        # raw_payload is intentionally omitted (provenance only; see PR-B endpoint).
        return {
            "id": self.id,
            "month": self.month,
            "component_kind": self.component_kind,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "amount_usd": _decimal_to_api(self.amount_usd),
            "amount_native": (
                None if self.amount_native is None else _decimal_to_api(self.amount_native)
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
    """Transform Google source rows into deduction components."""
    components: list[DeductionComponentInput] = []
    skipped_non_usd = 0
    for row in rows:
        if not _is_projecting_deduction_source_row(row):
            continue
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
    """Convert bank reconciliation entries into PAYMENT-scoped components."""
    components: list[DeductionComponentInput] = []
    for entry in entries:
        if entry.transfer_fee_usd > 0:
            components.append(
                _bank_component(
                    entry,
                    month,
                    "TRANSFER_FEE",
                    entry.transfer_fee_usd,
                    "transfer_fee",
                )
            )
        if entry.fx_difference_usd != 0:
            components.append(
                _bank_component(
                    entry,
                    month,
                    "FX_VARIANCE",
                    entry.fx_difference_usd,
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
    """Create one bank-derived deduction component."""
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
    """Map AdSense settled-vs-paid account gaps into components."""
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
