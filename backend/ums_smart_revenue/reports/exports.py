import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.db.report_models import ExportJobORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FINANCE_EXPORT_TYPES = frozenset(
    {"FINANCE_EXCEL", "EXECUTIVE_PDF", "BRANDED_SLIDE_PACK"}
)
ANALYTICS_EXPORT_TYPES = frozenset({"ANALYTICS_SUMMARY_CSV"})
ALLOWED_EXPORT_TYPES = FINANCE_EXPORT_TYPES | ANALYTICS_EXPORT_TYPES
ALLOWED_EXPORT_SCOPE_TYPES = frozenset(
    {"global", "sector", "company", "channel", "group"}
)
MAX_EXPORT_JOB_PAGE_SIZE = 100
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class ExportJobEntry:
    id: str
    export_type: str
    scope_type: str
    scope_id: str | None
    month: str
    currency: str
    requested_by: str
    status: str
    file_url: str | None
    month_lock_status: str
    include_confidence_notes: bool
    include_manual_override_notes: bool
    created_at: datetime
    completed_at: datetime | None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "export_type": self.export_type,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "month": self.month,
            "currency": self.currency,
            "requested_by": self.requested_by,
            "status": self.status,
            "file_url": self.file_url,
            "month_lock_status": self.month_lock_status,
            "include_confidence_notes": self.include_confidence_notes,
            "include_manual_override_notes": self.include_manual_override_notes,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
        }


@dataclass(frozen=True)
class ExportJobPage:
    items: list[ExportJobEntry]
    limit: int
    offset: int
    has_more: bool


class ExportJobError(ValueError):
    pass


class ExportJobNotFoundError(ExportJobError):
    pass


class ExportJobValidationError(ExportJobError):
    pass


class SqlAlchemyExportJobRepository:
    def __init__(self, session: Session):
        self._session = session
        self._tenant_id = _DEFAULT_TENANT_UUID

    def request_export(
        self,
        *,
        export_type: str,
        scope_type: str,
        scope_id: str | None,
        month: str,
        currency: str,
        actor_user_id: str,
        include_confidence_notes: bool,
        include_manual_override_notes: bool,
    ) -> ExportJobEntry:
        normalized_export_type = _normalize_export_type(export_type)
        normalized_scope_type, normalized_scope_id = _normalize_scope(
            scope_type, scope_id
        )
        _validate_month(month)
        normalized_currency = _normalize_currency(currency)
        actor_uuid = _parse_uuid(actor_user_id)
        month_lock_status = self._month_lock_status(month)

        row = ExportJobORM(
            id=uuid4(),
            export_type=normalized_export_type,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            month=month,
            currency=normalized_currency,
            requested_by=actor_uuid,
            status="QUEUED",
            file_url=None,
            month_lock_status=month_lock_status,
            include_confidence_notes=include_confidence_notes,
            include_manual_override_notes=include_manual_override_notes,
            tenant_id=self._tenant_id,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_entry(row)

    def get_job(self, export_id: str) -> ExportJobEntry:
        export_uuid = _parse_uuid(export_id, field_name="export_id")
        row = self._session.scalars(
            select(ExportJobORM).where(
                ExportJobORM.id == export_uuid,
                ExportJobORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None:
            raise ExportJobNotFoundError("Export job not found")
        return self._to_entry(row)

    def list_jobs(
        self,
        *,
        requested_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ExportJobPage:
        if limit < 1 or limit > MAX_EXPORT_JOB_PAGE_SIZE:
            raise ExportJobValidationError(
                f"limit must be between 1 and {MAX_EXPORT_JOB_PAGE_SIZE}"
            )
        if offset < 0:
            raise ExportJobValidationError("offset must be greater than or equal to 0")

        statement = (
            select(ExportJobORM)
            .where(ExportJobORM.tenant_id == self._tenant_id)
            .order_by(ExportJobORM.created_at.desc(), ExportJobORM.id.desc())
        )
        if requested_by is not None:
            statement = statement.where(
                ExportJobORM.requested_by == _parse_uuid(requested_by)
            )

        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
        return ExportJobPage(
            items=[self._to_entry(row) for row in rows[:limit]],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def _month_lock_status(self, month: str) -> str:
        row = self._session.get(FinanceMonthCloseORM, (self._tenant_id, month))
        return row.status if row is not None else "OPEN"

    @staticmethod
    def _to_entry(row: ExportJobORM) -> ExportJobEntry:
        return ExportJobEntry(
            id=str(row.id),
            export_type=row.export_type,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            month=row.month,
            currency=row.currency,
            requested_by=str(row.requested_by),
            status=row.status,
            file_url=row.file_url,
            month_lock_status=row.month_lock_status,
            include_confidence_notes=row.include_confidence_notes,
            include_manual_override_notes=row.include_manual_override_notes,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


def is_finance_export_type(export_type: str) -> bool:
    return export_type in FINANCE_EXPORT_TYPES


def _normalize_export_type(value: str) -> str:
    normalized = _normalize_required_string(value, "export_type")
    if normalized not in ALLOWED_EXPORT_TYPES:
        raise ExportJobValidationError(f"Unknown export_type: {value}")
    return normalized


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str | None]:
    normalized_scope_type = _normalize_required_string(scope_type, "scope_type")
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else scope_id
    if normalized_scope_type not in ALLOWED_EXPORT_SCOPE_TYPES:
        raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")
    if normalized_scope_type == "global":
        if normalized_scope_id:
            raise ExportJobValidationError(
                "scope_id must be omitted for global exports"
            )
        return normalized_scope_type, None
    if not normalized_scope_id:
        raise ExportJobValidationError(
            f"scope_id is required for export scope_type: {normalized_scope_type}"
        )
    return normalized_scope_type, normalized_scope_id


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise ExportJobValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_currency(value: str) -> str:
    normalized = _normalize_required_string(value, "currency").upper()
    if normalized != "USD":
        raise ExportJobValidationError(
            "currency must be USD until exchange-rate support is implemented"
        )
    return normalized


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ExportJobValidationError(f"{field_name} must not be blank")
    return normalized


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ExportJobValidationError(f"{field_name} must be a valid UUID") from exc
