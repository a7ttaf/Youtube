import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import AdSensePaymentORM
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
ALLOWED_PAYMENT_STATUSES = frozenset({"PAID", "PENDING", "UNPAID", "CANCELLED"})
MAX_ADSENSE_PAYMENT_PAGE_SIZE = 100
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class AdSensePaymentInput:
    """Validated repository input for one AdSense payment write."""

    source_account_id: str
    month: str
    payment_name: str
    payment_date: date
    payment_amount: Decimal
    payment_currency: str
    payment_status: str
    raw_payload: dict[str, object]


@dataclass(frozen=True)
class AdSensePaymentEntry:
    """Read-model representation of one persisted AdSense payment row."""

    id: str
    source_account_id: str
    month: str
    payment_name: str
    payment_date: date
    payment_amount: Decimal
    payment_currency: str
    payment_status: str
    raw_payload: dict[str, object]
    source_report_id: str | None
    imported_by: str | None

    @property
    def audit_entity_id(self) -> str:
        """Return a stable account-scoped audit entity identifier."""
        return f"{self.source_account_id}:{self.month}:{self.payment_name}"

    def to_api(self) -> dict[str, object]:
        """Serialize this payment entry into the public API response shape."""
        return {
            "id": self.id,
            "source_account_id": self.source_account_id,
            "month": self.month,
            "payment_name": self.payment_name,
            "payment_date": self.payment_date.isoformat(),
            "payment_amount": _decimal_to_api(self.payment_amount),
            "payment_currency": self.payment_currency,
            "payment_status": self.payment_status,
            "raw_payload": dict(self.raw_payload),
            "source_report_id": self.source_report_id,
            "imported_by": self.imported_by,
        }


@dataclass(frozen=True)
class AdSensePaymentPage:
    """Paginated AdSense payment read result."""

    items: list[AdSensePaymentEntry]
    limit: int
    offset: int
    has_more: bool


class AdSensePaymentError(ValueError):
    """Base exception for AdSense payment repository failures."""

    pass


class AdSensePaymentLockedMonthError(AdSensePaymentError):
    """Raised when a write targets a locked finance month."""

    pass


class AdSensePaymentValidationError(AdSensePaymentError):
    """Raised when payment repository input fails validation."""

    pass


class SqlAlchemyAdSensePaymentRepository:
    """SQLAlchemy repository for tenant-scoped AdSense payment source rows."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind the database session and resolved tenant scope."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def sync_payments(
        self,
        *,
        payments: list[AdSensePaymentInput],
        actor_user_id: str,
        source_report_id: str | None,
    ) -> list[AdSensePaymentEntry]:
        """Upsert account-scoped payment rows after validation and lock checks."""
        if not payments:
            raise AdSensePaymentValidationError(
                "payments must contain at least one payment"
            )
        actor_uuid = _parse_uuid_or_none(actor_user_id)
        normalized_source_report_id = _normalize_optional_string(source_report_id)

        normalized_payments: list[AdSensePaymentInput] = []
        seen_payment_keys: set[tuple[str, str, str]] = set()
        for payment in payments:
            normalized_payment = _normalize_payment(payment)
            payment_key = (
                normalized_payment.source_account_id,
                normalized_payment.month,
                normalized_payment.payment_name,
            )
            if payment_key in seen_payment_keys:
                raise AdSensePaymentValidationError(
                    "duplicate AdSense payment in batch: "
                    f"{normalized_payment.source_account_id}:"
                    f"{normalized_payment.month}:{normalized_payment.payment_name}"
                )
            seen_payment_keys.add(payment_key)
            normalized_payments.append(normalized_payment)

        entries: list[AdSensePaymentEntry] = []
        for normalized_payment in normalized_payments:
            self._require_month_open(normalized_payment.month)
            now = datetime.now(UTC)
            insert_statement = _dialect_insert(self._session.get_bind().dialect.name)(
                AdSensePaymentORM
            ).values(
                id=uuid4(),
                tenant_id=self._tenant_id,
                month=normalized_payment.month,
                payment_name=normalized_payment.payment_name,
                source_account_id=normalized_payment.source_account_id,
                payment_date=normalized_payment.payment_date,
                payment_amount=normalized_payment.payment_amount,
                payment_currency=normalized_payment.payment_currency,
                payment_status=normalized_payment.payment_status,
                raw_payload=dict(normalized_payment.raw_payload),
                source_report_id=normalized_source_report_id,
                imported_by=actor_uuid,
                updated_at=now,
            )
            update_values: dict[str, object] = {
                "payment_date": normalized_payment.payment_date,
                "payment_amount": normalized_payment.payment_amount,
                "payment_currency": normalized_payment.payment_currency,
                "payment_status": normalized_payment.payment_status,
                "raw_payload": dict(normalized_payment.raw_payload),
                "source_report_id": normalized_source_report_id,
                "updated_at": now,
            }
            # Preserve existing imported_by attribution when the current actor
            # cannot be resolved to a UUID. Setting imported_by=None on conflict
            # would erase the original importer.
            if actor_uuid is not None:
                update_values["imported_by"] = actor_uuid
            statement = insert_statement.on_conflict_do_update(
                index_elements=[
                    AdSensePaymentORM.tenant_id,
                    AdSensePaymentORM.source_account_id,
                    AdSensePaymentORM.month,
                    AdSensePaymentORM.payment_name,
                ],
                set_=update_values,
            ).returning(AdSensePaymentORM.id)
            row_id = self._session.execute(statement).scalar_one()
            row = self._session.get(AdSensePaymentORM, row_id)
            if row is None:
                raise AdSensePaymentValidationError("AdSense payment upsert failed")
            self._session.refresh(row)
            entries.append(self._to_entry(row))

        return entries

    def list_payments(
        self,
        *,
        month: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AdSensePaymentPage:
        """Return a tenant-filtered, total-ordered page of payment rows."""
        if limit < 1 or limit > MAX_ADSENSE_PAYMENT_PAGE_SIZE:
            raise AdSensePaymentValidationError(
                f"limit must be between 1 and {MAX_ADSENSE_PAYMENT_PAGE_SIZE}"
            )
        if offset < 0:
            raise AdSensePaymentValidationError(
                "offset must be greater than or equal to 0"
            )
        if month is not None:
            _validate_month(month)

        statement = (
            select(AdSensePaymentORM)
            .where(AdSensePaymentORM.tenant_id == self._tenant_id)
            .order_by(
                AdSensePaymentORM.month.desc(),
                AdSensePaymentORM.payment_date.desc(),
                AdSensePaymentORM.payment_name,
                AdSensePaymentORM.source_account_id,
                AdSensePaymentORM.id,
            )
        )
        if month is not None:
            statement = statement.where(AdSensePaymentORM.month == month)

        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
        return AdSensePaymentPage(
            items=[self._to_entry(row) for row in rows[:limit]],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def list_month_payments(self, *, month: str) -> list[AdSensePaymentEntry]:
        """Return all tenant payment rows for a month in stable display order."""
        _validate_month(month)
        rows = self._session.scalars(
            select(AdSensePaymentORM)
            .where(AdSensePaymentORM.tenant_id == self._tenant_id)
            .where(AdSensePaymentORM.month == month)
            .order_by(
                AdSensePaymentORM.payment_date.desc(),
                AdSensePaymentORM.payment_name,
                AdSensePaymentORM.source_account_id,
                AdSensePaymentORM.id,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    def _require_month_open(self, month: str) -> None:
        """Lock and verify the target finance month before writing."""
        close = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close.status == "LOCKED":
            raise AdSensePaymentLockedMonthError(
                "Finance month is locked for AdSense payment sync"
            )

    @staticmethod
    def _to_entry(row: AdSensePaymentORM) -> AdSensePaymentEntry:
        """Convert an ORM row into the repository read model."""
        return AdSensePaymentEntry(
            id=str(row.id),
            source_account_id=row.source_account_id,
            month=row.month,
            payment_name=row.payment_name,
            payment_date=row.payment_date,
            payment_amount=row.payment_amount,
            payment_currency=row.payment_currency,
            payment_status=row.payment_status,
            raw_payload=dict(row.raw_payload or {}),
            source_report_id=row.source_report_id,
            imported_by=str(row.imported_by) if row.imported_by else None,
        )


def _normalize_payment(payment: AdSensePaymentInput) -> AdSensePaymentInput:
    """Normalize and validate one payment input without mutating the caller."""
    _validate_month(payment.month)
    source_account_id = _normalize_required_string(
        payment.source_account_id, "source_account_id"
    )
    payment_name = _normalize_required_string(payment.payment_name, "payment_name")
    payment_currency = _normalize_currency(payment.payment_currency)
    payment_status = _normalize_payment_status(payment.payment_status)
    _validate_payment_amount(payment.payment_amount)
    if not isinstance(payment.raw_payload, dict):
        raise AdSensePaymentValidationError("raw_payload must be an object")
    return AdSensePaymentInput(
        source_account_id=source_account_id,
        month=payment.month,
        payment_name=payment_name,
        payment_date=payment.payment_date,
        payment_amount=payment.payment_amount,
        payment_currency=payment_currency,
        payment_status=payment_status,
        raw_payload=dict(payment.raw_payload),
    )


def _validate_month(month: str) -> None:
    """Validate a YYYY-MM finance month string."""
    if not MONTH_PATTERN.fullmatch(month):
        raise AdSensePaymentValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_required_string(value: str, field_name: str) -> str:
    """Trim a required string and reject blanks."""
    normalized = value.strip()
    if not normalized:
        raise AdSensePaymentValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_optional_string(value: str | None) -> str | None:
    """Trim an optional string and collapse blanks to None."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_currency(value: str) -> str:
    """Normalize an ISO currency code to uppercase."""
    normalized = _normalize_required_string(value, "payment_currency").upper()
    if not CURRENCY_PATTERN.fullmatch(normalized):
        raise AdSensePaymentValidationError(
            "payment_currency must be a three-letter ISO currency code"
        )
    return normalized


def _normalize_payment_status(value: str) -> str:
    """Normalize and validate the stored AdSense payment status."""
    normalized = _normalize_required_string(value, "payment_status").upper()
    if normalized not in ALLOWED_PAYMENT_STATUSES:
        raise AdSensePaymentValidationError(f"Unknown AdSense payment_status: {value}")
    return normalized


def _validate_payment_amount(value: Decimal) -> None:
    """Validate that a payment amount is finite and non-negative."""
    if not value.is_finite() or value < 0:
        raise AdSensePaymentValidationError(
            "payment_amount must be a finite decimal >= 0"
        )


def _dialect_insert(dialect_name: str):
    """Return the upsert-capable insert builder for the active SQL dialect."""
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    raise AdSensePaymentValidationError(
        f"Unsupported database dialect for AdSense payment upsert: {dialect_name}"
    )


def _parse_uuid_or_none(value: str) -> UUID | None:
    """Parse a UUID string, returning None for non-UUID service actors."""
    normalized = _normalize_required_string(value, "actor_user_id")
    try:
        return UUID(normalized)
    except ValueError:
        return None


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve the repository tenant from explicit, context, or default scope."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Parse a tenant UUID value for repository scoping."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise AdSensePaymentValidationError("tenant_id must be a valid UUID") from exc


def _decimal_to_api(value: Decimal) -> str:
    """Serialize a Decimal for JSON without losing precision."""
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
