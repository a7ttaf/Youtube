import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import BankReconciliationEntryORM
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.finance.reconciliation import ReconciliationIssue
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD = Decimal("0.01")
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class BankReconciliationError(ValueError):
    """Base exception for bank reconciliation errors."""
    pass


class BankReconciliationLockedMonthError(BankReconciliationError):
    """Exception for operations on a locked reconciliation month."""
    pass


class BankReconciliationValidationError(BankReconciliationError):
    """Exception raised when provided data fails bank reconciliation validation checks."""
    pass


@dataclass(frozen=True)
class BankReconciliationEntry:
    """Data class representing a single bank reconciliation entry with detailed information."""
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
        """Convert the bank reconciliation record to a JSON-serializable dictionary for API responses."""
        return {
            "id": self.id,
            "month": self.month,
            "bank_reference": self.bank_reference,
            "bank_received_date": self.bank_received_date.isoformat(),
            "bank_received_amount": _decimal_to_api(self.bank_received_amount),
            "bank_received_currency": self.bank_received_currency,
            "bank_received_amount_usd": _decimal_to_api(self.bank_received_amount_usd),
            "transfer_fee_usd": _decimal_to_api(self.transfer_fee_usd),
            "fx_difference_usd": _decimal_to_api(self.fx_difference_usd),
            "notes": self.notes,
            "source_report_id": self.source_report_id,
            "recorded_by": self.recorded_by,
        }


@dataclass(frozen=True)
class MonthBankReconciliationSummary:
    """Data class summarizing bank reconciliation data for a specific month, including totals and issues."""

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
        """Convert the reconciliation summary into an API-friendly dictionary."""
        return {
            "month": self.month,
            "currency": self.currency,
            "status": self.status,
            "adsense_paid_amount_usd": _decimal_to_api(self.adsense_paid_amount_usd),
            "bank_received_amount_usd": _decimal_to_api(self.bank_received_amount_usd),
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
    """SQLAlchemy-based repository for recording and retrieving bank reconciliation entries."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

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
        """Create and persist a new bank reconciliation entry with the specified data."""
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
        actor_uuid = _actor_identity_uuid(actor_user_id)
        self._require_month_open(month)

        now = datetime.now(UTC)
        insert_statement = _dialect_insert(self._session.get_bind().dialect.name)(
            BankReconciliationEntryORM
        ).values(
            id=uuid4(),
            tenant_id=self._tenant_id,
            month=month,
            bank_reference=normalized_reference,
            bank_received_date=bank_received_date,
            bank_received_amount=bank_received_amount,
            bank_received_currency=normalized_currency,
            bank_received_amount_usd=bank_received_amount_usd,
            transfer_fee_usd=transfer_fee_usd,
            fx_difference_usd=fx_difference_usd,
            notes=normalized_notes,
            source_report_id=normalized_source_report_id,
            recorded_by=actor_uuid,
            updated_at=now,
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[
                BankReconciliationEntryORM.tenant_id,
                BankReconciliationEntryORM.month,
                BankReconciliationEntryORM.bank_reference,
            ],
            set_={
                "bank_received_date": bank_received_date,
                "bank_received_amount": bank_received_amount,
                "bank_received_currency": normalized_currency,
                "bank_received_amount_usd": bank_received_amount_usd,
                "transfer_fee_usd": transfer_fee_usd,
                "fx_difference_usd": fx_difference_usd,
                "notes": normalized_notes,
                "source_report_id": normalized_source_report_id,
                "recorded_by": actor_uuid,
                "updated_at": now,
            },
        ).returning(BankReconciliationEntryORM.id)
        row_id = self._session.execute(statement).scalar_one()
        row = self._session.get(BankReconciliationEntryORM, row_id)
        if row is None:
            raise BankReconciliationValidationError("Bank reconciliation upsert failed")
        self._session.refresh(row)
        return self._to_entry(row)

    def list_month_entries(self, *, month: str) -> list[BankReconciliationEntry]:
        """List bank reconciliation entries for the specified month.

        Args:
            month (str): The month in YYYY-MM format to retrieve entries for.

        Returns:
            list[BankReconciliationEntry]: A list of bank reconciliation entries for the given month.
        """
        _validate_month(month)
        rows = self._session.scalars(
            select(BankReconciliationEntryORM)
            .where(
                BankReconciliationEntryORM.tenant_id == self._tenant_id,
                BankReconciliationEntryORM.month == month,
            )
            .order_by(
                BankReconciliationEntryORM.bank_received_date.desc(),
                BankReconciliationEntryORM.bank_reference,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    def _require_month_open(self, month: str) -> None:
        """Ensure that the specified finance month is open for reconciliation.

        Args:
            month (str): The month in YYYY-MM format to check.

        Raises:
            BankReconciliationLockedMonthError: If the finance month is locked.
        """
        close = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close.status == "LOCKED":
            raise BankReconciliationLockedMonthError(
                "Finance month is locked for bank reconciliation"
            )

    @staticmethod
    def _to_entry(row: BankReconciliationEntryORM) -> BankReconciliationEntry:
        """Convert a BankReconciliationEntryORM row to a domain entry object.

        Args:
            row (BankReconciliationEntryORM): The ORM row to convert.

        Returns:
            BankReconciliationEntry: The domain representation of the bank reconciliation entry.
        """
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


def _filter_by_month(items, month):
    return [item for item in items if item.month == month]


def _filter_usd_payments(payments):
    return [payment for payment in payments if payment.payment_currency == "USD"]


def _filter_paid_payments(payments):
    return [payment for payment in payments if payment.payment_status == "PAID"]


def _sum_and_quantize(items, attr):
    return _quantize_money(
        sum((getattr(item, attr) for item in items), Decimal("0"))
    )


def build_month_bank_reconciliation_summary(
    *,
    month: str,
    payments: Iterable[AdSensePaymentEntry],
    bank_entries: Iterable[BankReconciliationEntry],
    tolerance_usd: Decimal = DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD,
) -> MonthBankReconciliationSummary:
    """Build a summary of bank reconciliation for a given month.

    Filters AdSense payments and bank entries for the month and computes reconciliation metrics.

    Args:
        month (str): The month in YYYY-MM format for the summary.
        payments (Iterable[AdSensePaymentEntry]): All AdSense payment entries.
        bank_entries (Iterable[BankReconciliationEntry]): All bank reconciliation entries.
        tolerance_usd (Decimal): The USD tolerance for reconciliation discrepancies.

    Returns:
        MonthBankReconciliationSummary: A summary of the bank reconciliation for the month.
    """
    month_payments = _filter_by_month(payments, month)
    month_entries = _filter_by_month(bank_entries, month)
    usd_payments = _filter_usd_payments(month_payments)
    paid_payments = _filter_paid_payments(usd_payments)
    non_paid_payment_count = len(usd_payments) - len(paid_payments)
    unsupported_payment_currency_count = len(month_payments) - len(usd_payments)

    adsense_paid_amount = _sum_and_quantize(paid_payments, "payment_amount")
    bank_received_amount_usd = _sum_and_quantize(
        month_entries, "bank_received_amount_usd"
    )
    transfer_fee_usd = _sum_and_quantize(month_entries, "transfer_fee_usd")
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
    """
    Validate that the provided month string follows YYYY-MM format and represents a valid calendar month from 01 to 12.
    """
    if not MONTH_PATTERN.fullmatch(month):
        raise BankReconciliationValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_currency(value: str) -> str:
    """
    Normalize and validate a required currency code string to a three-letter uppercase ISO code.
    """
    normalized = _normalize_required_string(value, "bank_received_currency").upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise BankReconciliationValidationError(
            "bank_received_currency must be a three-letter ISO currency code"
        )
    return normalized


def _normalize_required_string(value: str, field_name: str) -> str:
    """
    Trim whitespace from a required string field and ensure it is not blank; raise an error if empty.
    """
    normalized = value.strip()
    if not normalized:
        raise BankReconciliationValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_optional_string(value: str | None) -> str | None:
    """
    Trim whitespace from an optional string field and return None if resulting string is empty or input is None.
    """
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_nonnegative_money(value: Decimal, field_name: str) -> None:
    """
    Ensure a monetary Decimal value is finite and not negative; raise an error if invalid.
    """
    _validate_finite_money(value, field_name)
    if value < 0:
        raise BankReconciliationValidationError(f"{field_name} must be nonnegative")


def _validate_finite_money(value: Decimal, field_name: str) -> None:
    """
    Ensure a monetary Decimal value is finite; raise an error if it is infinite or NaN.
    """
    if not value.is_finite():
        raise BankReconciliationValidationError(f"{field_name} must be finite")


def _actor_identity_uuid(value: str) -> UUID:
    """
    Parse an actor identity string into a UUID.
    """
    # Accept either a UUID literal or a trusted-gateway subject; the shared
    # helper derives a deterministic UUID5 for the latter so header-auth
    # deployments with non-UUID x-user-id values can still record bank
    # reconciliation entries.
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise BankReconciliationValidationError(str(exc)) from exc


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    """
    Convert a string to UUID, raising a validation error if the input is not a valid UUID format.
    """
    try:
        return UUID(value)
    except ValueError as exc:
        raise BankReconciliationValidationError(
            f"{field_name} must be a valid UUID"
        ) from exc


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """
    Determine and return the tenant UUID: parse provided ID, use current tenant context, or fall back to default.
    """
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """
    Parse and return a tenant UUID from a UUID instance or string, raising on invalid format.
    """
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise BankReconciliationValidationError(
            "tenant_id must be a valid UUID"
        ) from exc


def _quantize_money(value: Decimal) -> Decimal:
    """
    Quantize a Decimal monetary value to four decimal places using half-up rounding.
    """
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _dialect_insert(dialect_name: str):
    """Select the SQLAlchemy insert function for the current database dialect."""
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    raise BankReconciliationValidationError(
        f"Unsupported database dialect for bank reconciliation upsert: {dialect_name}"
    )
