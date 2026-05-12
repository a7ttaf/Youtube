import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import BankReconciliationEntryORM
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.finance.reconciliation import ReconciliationIssue

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD = Decimal("0.01")


class BankReconciliationError(ValueError):
    pass


class BankReconciliationLockedMonthError(BankReconciliationError):
    pass


class BankReconciliationValidationError(BankReconciliationError):
    pass


@dataclass(frozen=True)
class BankReconciliationEntry:
    id: str
    month: str
    bank_reference: str
    bank_received_date: date
    bank_received_amount: Decimal
    bank_received_currency: str
    bank_received_amount_usd: Decimal
    transfer_fee_usd: Decimal
    fx_difference_usd: Decimal
    notes: str | None
    source_report_id: str | None
    recorded_by: str

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "month": self.month,
            "bank_reference": self.bank_reference,
            "bank_received_date": self.bank_received_date.isoformat(),
            "bank_received_amount": _decimal_to_api(self.bank_received_amount),
            "bank_received_currency": self.bank_received_currency,
            "bank_received_amount_usd": _decimal_to_api(
                self.bank_received_amount_usd
            ),
            "transfer_fee_usd": _decimal_to_api(self.transfer_fee_usd),
            "fx_difference_usd": _decimal_to_api(self.fx_difference_usd),
            "notes": self.notes,
            "source_report_id": self.source_report_id,
            "recorded_by": self.recorded_by,
        }


@dataclass(frozen=True)
class MonthBankReconciliationSummary:
    month: str
    currency: str
    status: str
    adsense_paid_amount_usd: Decimal
    bank_received_amount_usd: Decimal
    bank_gap_usd: Decimal | None
    transfer_fee_usd: Decimal
    fx_difference_usd: Decimal
    payment_count: int
    paid_payment_count: int
    non_paid_payment_count: int
    unsupported_payment_currency_count: int
    entry_count: int
    tolerance_usd: Decimal
    issues: list[ReconciliationIssue]
    entries: list[BankReconciliationEntry]

    def to_api(self) -> dict[str, object]:
        return {
            "month": self.month,
            "currency": self.currency,
            "status": self.status,
            "adsense_paid_amount_usd": _decimal_to_api(
                self.adsense_paid_amount_usd
            ),
            "bank_received_amount_usd": _decimal_to_api(
                self.bank_received_amount_usd
            ),
            "bank_gap_usd": _decimal_to_api(self.bank_gap_usd),
            "transfer_fee_usd": _decimal_to_api(self.transfer_fee_usd),
            "fx_difference_usd": _decimal_to_api(self.fx_difference_usd),
            "payment_count": self.payment_count,
            "paid_payment_count": self.paid_payment_count,
            "non_paid_payment_count": self.non_paid_payment_count,
            "unsupported_payment_currency_count": (
                self.unsupported_payment_currency_count
            ),
            "entry_count": self.entry_count,
            "tolerance_usd": _decimal_to_api(self.tolerance_usd),
            "issues": [issue.to_api() for issue in self.issues],
            "entries": [entry.to_api() for entry in self.entries],
        }


class SqlAlchemyBankReconciliationRepository:
    def __init__(self, session: Session):
        self._session = session

    def record_entry(
        self,
        *,
        month: str,
        bank_reference: str,
        bank_received_date: date,
        bank_received_amount: Decimal,
        bank_received_currency: str,
        bank_received_amount_usd: Decimal,
        transfer_fee_usd: Decimal,
        fx_difference_usd: Decimal,
        notes: str | None,
        source_report_id: str | None,
        actor_user_id: str,
    ) -> BankReconciliationEntry:
        _validate_month(month)
        normalized_reference = _normalize_required_string(
            bank_reference,
            "bank_reference",
        )
        normalized_currency = _normalize_currency(bank_received_currency)
        normalized_notes = _normalize_optional_string(notes)
        normalized_source_report_id = _normalize_optional_string(source_report_id)
        _validate_nonnegative_money(bank_received_amount, "bank_received_amount")
        _validate_nonnegative_money(
            bank_received_amount_usd,
            "bank_received_amount_usd",
        )
        _validate_nonnegative_money(transfer_fee_usd, "transfer_fee_usd")
        _validate_finite_money(fx_difference_usd, "fx_difference_usd")
        actor_uuid = _parse_uuid(actor_user_id)
        self._require_month_open(month)

        row = self._session.scalars(
            select(BankReconciliationEntryORM)
            .where(
                BankReconciliationEntryORM.month == month,
                BankReconciliationEntryORM.bank_reference == normalized_reference,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            row = BankReconciliationEntryORM(
                id=uuid4(),
                month=month,
                bank_reference=normalized_reference,
            )
            self._session.add(row)

        row.bank_received_date = bank_received_date
        row.bank_received_amount = bank_received_amount
        row.bank_received_currency = normalized_currency
        row.bank_received_amount_usd = bank_received_amount_usd
        row.transfer_fee_usd = transfer_fee_usd
        row.fx_difference_usd = fx_difference_usd
        row.notes = normalized_notes
        row.source_report_id = normalized_source_report_id
        row.recorded_by = actor_uuid
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_entry(row)

    def list_month_entries(self, *, month: str) -> list[BankReconciliationEntry]:
        _validate_month(month)
        rows = self._session.scalars(
            select(BankReconciliationEntryORM)
            .where(BankReconciliationEntryORM.month == month)
            .order_by(
                BankReconciliationEntryORM.bank_received_date.desc(),
                BankReconciliationEntryORM.bank_reference,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    def _require_month_open(self, month: str) -> None:
        close = get_or_create_month_close_row(self._session, month, for_update=True)
        if close.status == "LOCKED":
            raise BankReconciliationLockedMonthError(
                "Finance month is locked for bank reconciliation"
            )

    @staticmethod
    def _to_entry(row: BankReconciliationEntryORM) -> BankReconciliationEntry:
        return BankReconciliationEntry(
            id=str(row.id),
            month=row.month,
            bank_reference=row.bank_reference,
            bank_received_date=row.bank_received_date,
            bank_received_amount=row.bank_received_amount,
            bank_received_currency=row.bank_received_currency,
            bank_received_amount_usd=row.bank_received_amount_usd,
            transfer_fee_usd=row.transfer_fee_usd,
            fx_difference_usd=row.fx_difference_usd,
            notes=row.notes,
            source_report_id=row.source_report_id,
            recorded_by=str(row.recorded_by),
        )


def build_month_bank_reconciliation_summary(
    *,
    month: str,
    payments: Iterable[AdSensePaymentEntry],
    bank_entries: Iterable[BankReconciliationEntry],
    tolerance_usd: Decimal = DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD,
) -> MonthBankReconciliationSummary:
    month_payments = [payment for payment in payments if payment.month == month]
    month_entries = [entry for entry in bank_entries if entry.month == month]
    usd_payments = [
        payment for payment in month_payments if payment.payment_currency == "USD"
    ]
    paid_payments = [
        payment for payment in usd_payments if payment.payment_status == "PAID"
    ]
    non_paid_payment_count = len(usd_payments) - len(paid_payments)
    unsupported_payment_currency_count = len(month_payments) - len(usd_payments)

    adsense_paid_amount = _quantize_money(
        sum((payment.payment_amount for payment in paid_payments), Decimal("0"))
    )
    bank_received_amount_usd = _quantize_money(
        sum((entry.bank_received_amount_usd for entry in month_entries), Decimal("0"))
    )
    transfer_fee_usd = _quantize_money(
        sum((entry.transfer_fee_usd for entry in month_entries), Decimal("0"))
    )
    fx_difference_usd = _quantize_money(
        sum((entry.fx_difference_usd for entry in month_entries), Decimal("0"))
    )

    issues: list[ReconciliationIssue] = []
    bank_gap: Decimal | None = None
    if not paid_payments:
        status = "MISSING_ADSENSE_PAYMENT"
        issues.append(
            ReconciliationIssue(
                issue_type="MISSING_ADSENSE_PAYMENT",
                severity="HIGH",
                message=f"No paid USD AdSense payment is available for {month}.",
            )
        )
    elif not month_entries:
        status = "MISSING_BANK_RECEIPT"
        issues.append(
            ReconciliationIssue(
                issue_type="MISSING_BANK_RECEIPT",
                severity="HIGH",
                message=f"No bank receipt is recorded for {month}.",
            )
        )
    else:
        bank_gap = _quantize_money(adsense_paid_amount - bank_received_amount_usd)
        if abs(bank_gap) <= tolerance_usd:
            status = "BANK_CONFIRMED"
        else:
            status = "BANK_VARIANCE"
            issues.append(
                ReconciliationIssue(
                    issue_type="BANK_GAP",
                    severity="HIGH",
                    message=(
                        "Paid AdSense and normalized bank receipts differ by "
                        f"{_decimal_to_api(abs(bank_gap))} for {month}."
                    ),
                )
            )

    if non_paid_payment_count:
        issues.append(
            ReconciliationIssue(
                issue_type="NON_PAID_ADSENSE_PAYMENTS",
                severity="MEDIUM",
                message=(
                    f"{non_paid_payment_count} USD AdSense payment record(s) "
                    f"for {month} are not PAID and were excluded."
                ),
            )
        )
    if unsupported_payment_currency_count:
        issues.append(
            ReconciliationIssue(
                issue_type="UNSUPPORTED_PAYMENT_CURRENCY",
                severity="HIGH",
                message=(
                    f"{unsupported_payment_currency_count} AdSense payment "
                    f"record(s) for {month} are not USD and were excluded."
                ),
            )
        )

    return MonthBankReconciliationSummary(
        month=month,
        currency="USD",
        status=status,
        adsense_paid_amount_usd=adsense_paid_amount,
        bank_received_amount_usd=bank_received_amount_usd,
        bank_gap_usd=bank_gap,
        transfer_fee_usd=transfer_fee_usd,
        fx_difference_usd=fx_difference_usd,
        payment_count=len(month_payments),
        paid_payment_count=len(paid_payments),
        non_paid_payment_count=non_paid_payment_count,
        unsupported_payment_currency_count=unsupported_payment_currency_count,
        entry_count=len(month_entries),
        tolerance_usd=tolerance_usd,
        issues=issues,
        entries=sorted(
            month_entries,
            key=lambda entry: (entry.bank_received_date, entry.bank_reference),
            reverse=True,
        ),
    )


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise BankReconciliationValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_currency(value: str) -> str:
    normalized = _normalize_required_string(value, "bank_received_currency").upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise BankReconciliationValidationError(
            "bank_received_currency must be a three-letter ISO currency code"
        )
    return normalized


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise BankReconciliationValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_nonnegative_money(value: Decimal, field_name: str) -> None:
    _validate_finite_money(value, field_name)
    if value < 0:
        raise BankReconciliationValidationError(f"{field_name} must be nonnegative")


def _validate_finite_money(value: Decimal, field_name: str) -> None:
    if not value.is_finite():
        raise BankReconciliationValidationError(f"{field_name} must be finite")


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise BankReconciliationValidationError(
            f"{field_name} must be a valid UUID"
        ) from exc


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
