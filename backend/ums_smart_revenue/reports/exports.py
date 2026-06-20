import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.db.report_models import ExportJobORM, ExportTemplateORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FINANCE_EXPORT_TYPES = frozenset({"FINANCE_EXCEL", "EXECUTIVE_PDF", "BRANDED_SLIDE_PACK"})
ANALYTICS_EXPORT_TYPES = frozenset({"ANALYTICS_SUMMARY_CSV"})
ALLOWED_EXPORT_TYPES = FINANCE_EXPORT_TYPES | ANALYTICS_EXPORT_TYPES
ALLOWED_EXPORT_SCOPE_TYPES = frozenset({"global", "sector", "company", "channel", "group"})
ALLOWED_EXPORT_ARTIFACT_URI_PREFIXES = (
    "file-store://",
    "s3://",
    "gs://",
    "azure://",
    "blob://",
)
MAX_EXPORT_JOB_PAGE_SIZE = 100
MAX_EXPORT_TEMPLATE_PAGE_SIZE = 100
_TERMINAL_EXPORT_JOB_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
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
    artifact_filename: str | None = None
    artifact_content_type: str | None = None
    artifact_byte_size: int | None = None
    artifact_checksum_sha256: str | None = None
    failure_reason: str | None = None
    # Channel set resolved and frozen at job creation; None means global
    # (all channels) or pre-snapshot legacy rows.
    scope_channel_ids: tuple[str, ...] | None = None
    template_id: str | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "template_id": self.template_id,
            "export_type": self.export_type,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "scope_channel_ids": (
                list(self.scope_channel_ids) if self.scope_channel_ids is not None else None
            ),
            "month": self.month,
            "currency": self.currency,
            "requested_by": self.requested_by,
            "status": self.status,
            "file_url": self.file_url,
            "artifact_filename": self.artifact_filename,
            "artifact_content_type": self.artifact_content_type,
            "artifact_byte_size": self.artifact_byte_size,
            "artifact_checksum_sha256": self.artifact_checksum_sha256,
            "failure_reason": self.failure_reason,
            "month_lock_status": self.month_lock_status,
            "include_confidence_notes": self.include_confidence_notes,
            "include_manual_override_notes": self.include_manual_override_notes,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass(frozen=True)
class ExportJobPage:
    items: list[ExportJobEntry]
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class ExportJobVisibilityFilter:
    export_types: frozenset[str]
    scope_type: str | None = None
    scope_id: str | None = None
    months: frozenset[str] | None = None


@dataclass(frozen=True)
class ExportTemplateEntry:
    id: str
    name: str
    export_type: str
    description: str | None
    layout_config: dict[str, object]
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: datetime

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "export_type": self.export_type,
            "description": self.description,
            "layout_config": self.layout_config,
            "is_active": self.is_active,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ExportTemplatePage:
    items: list[ExportTemplateEntry]
    limit: int
    offset: int
    has_more: bool


class ExportJobError(ValueError):
    pass


class ExportJobNotFoundError(ExportJobError):
    pass


class ExportJobValidationError(ExportJobError):
    pass


class ExportJobTerminalStateError(ExportJobValidationError):
    """Raised when a terminal-status job is asked to transition again."""


class ExportTemplateError(ValueError):
    pass


class ExportTemplateNotFoundError(ExportTemplateError):
    pass


class ExportTemplateValidationError(ExportTemplateError):
    pass


class SqlAlchemyExportTemplateRepository:
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind export template reads and writes to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    # ========================================================================
    # Purpose: Persist reusable export layout configuration while keeping
    #   finance and analytics source selection controlled by export jobs.
    # Database/ORM: INSERT INTO export_templates.
    # Standards: Tenant-scoped duplicate guard, JSON-serializable config, and
    #   actor identity normalization.
    # Blast Radius: Export configuration and audit only.
    # ========================================================================
    def create_template(
        self,
        *,
        name: str,
        export_type: str,
        layout_config: dict[str, object],
        actor_user_id: str,
        description: str | None = None,
    ) -> ExportTemplateEntry:
        normalized_name = _normalize_template_name(name)
        normalized_export_type = _normalize_template_export_type(export_type)
        normalized_layout_config = _normalize_template_layout_config(layout_config)
        normalized_description = _normalize_optional_string(description, "description")
        actor_uuid = _actor_identity_uuid(actor_user_id)
        self._ensure_template_name_available(normalized_name)
        row = ExportTemplateORM(
            id=uuid4(),
            name=normalized_name,
            export_type=normalized_export_type,
            description=normalized_description,
            layout_config=normalized_layout_config,
            is_active=True,
            created_by=actor_uuid,
            tenant_id=self._tenant_id,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_template_entry(row)

    def get_template(
        self, template_id: str, *, include_inactive: bool = True
    ) -> ExportTemplateEntry:
        row = self._get_template_row(template_id, include_inactive=include_inactive)
        return self._to_template_entry(row)

    def list_templates(
        self,
        *,
        export_type: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> ExportTemplatePage:
        if limit < 1 or limit > MAX_EXPORT_TEMPLATE_PAGE_SIZE:
            raise ExportTemplateValidationError(
                f"limit must be between 1 and {MAX_EXPORT_TEMPLATE_PAGE_SIZE}"
            )
        if offset < 0:
            raise ExportTemplateValidationError("offset must be greater than or equal to 0")
        statement = select(ExportTemplateORM).where(ExportTemplateORM.tenant_id == self._tenant_id)
        if export_type is not None:
            statement = statement.where(
                ExportTemplateORM.export_type == _normalize_template_export_type(export_type)
            )
        if not include_inactive:
            statement = statement.where(ExportTemplateORM.is_active.is_(True))
        statement = statement.order_by(
            ExportTemplateORM.created_at.desc(), ExportTemplateORM.id.desc()
        )
        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
        return ExportTemplatePage(
            items=[self._to_template_entry(row) for row in rows[:limit]],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    # ========================================================================
    # Purpose: Update operator-owned export template metadata without rewriting
    #   historical export jobs or changing the template export_type contract.
    # Database/ORM: UPDATE export_templates by tenant and template id.
    # Standards: Tenant-scoped lookup, duplicate-name guard, and JSON shape
    #   validation before persistence.
    # Blast Radius: Export configuration and audit only.
    # ========================================================================
    def update_template(
        self,
        template_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        description_provided: bool = False,
        layout_config: dict[str, object] | None = None,
        layout_config_provided: bool = False,
        is_active: bool | None = None,
    ) -> ExportTemplateEntry:
        row = self._get_template_row(template_id, include_inactive=True, for_update=True)
        if name is not None:
            normalized_name = _normalize_template_name(name)
            self._ensure_template_name_available(normalized_name, excluding_id=row.id)
            row.name = normalized_name
        if description_provided:
            row.description = _normalize_optional_string(description, "description")
        if layout_config_provided:
            if layout_config is None:
                raise ExportTemplateValidationError("layout_config must be an object")
            row.layout_config = _normalize_template_layout_config(layout_config)
        if is_active is not None:
            row.is_active = bool(is_active)
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_template_entry(row)

    def deactivate_template(self, template_id: str) -> ExportTemplateEntry:
        row = self._get_template_row(template_id, include_inactive=True, for_update=True)
        row.is_active = False
        row.updated_at = datetime.now(UTC)
        self._session.flush()
        return self._to_template_entry(row)

    def _get_template_row(
        self,
        template_id: str,
        *,
        include_inactive: bool,
        for_update: bool = False,
    ) -> ExportTemplateORM:
        template_uuid = _parse_uuid_for_template(template_id, field_name="template_id")
        statement = select(ExportTemplateORM).where(
            ExportTemplateORM.id == template_uuid,
            ExportTemplateORM.tenant_id == self._tenant_id,
        )
        if not include_inactive:
            statement = statement.where(ExportTemplateORM.is_active.is_(True))
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            raise ExportTemplateNotFoundError("Export template not found")
        return row

    def _ensure_template_name_available(
        self,
        name: str,
        *,
        excluding_id: UUID | None = None,
    ) -> None:
        statement = select(ExportTemplateORM.id).where(
            ExportTemplateORM.tenant_id == self._tenant_id,
            ExportTemplateORM.name == name,
        )
        if excluding_id is not None:
            statement = statement.where(ExportTemplateORM.id != excluding_id)
        existing_id = self._session.execute(statement).scalar_one_or_none()
        if existing_id is not None:
            raise ExportTemplateValidationError("Export template name already exists")

    @staticmethod
    def _to_template_entry(row: ExportTemplateORM) -> ExportTemplateEntry:
        layout_config = _clone_json_object(row.layout_config)
        return ExportTemplateEntry(
            id=str(row.id),
            name=row.name,
            export_type=row.export_type,
            description=row.description,
            layout_config=layout_config,
            is_active=row.is_active,
            created_by=str(row.created_by),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SqlAlchemyExportJobRepository:
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind export job reads and writes to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    # ========================================================================
    # Purpose: Create an export job and freeze its resolved channel set so
    #   later membership edits to the source group/sector/company cannot
    #   change the data returned for the same export_id.
    # Database/ORM: INSERT INTO export_jobs (..., scope_channel_ids).
    # Standards: Validated inputs, parameterized SQLAlchemy ORM, typed
    #   ExportJobValidationError at the boundary.
    # Blast Radius: Finance numbers, audit trail, artifact generation.
    # ========================================================================
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
        scope_channel_ids: tuple[str, ...] | frozenset[str] | None = None,
        template_id: str | None = None,
    ) -> ExportJobEntry:
        normalized_export_type = _normalize_export_type(export_type)
        normalized_scope_type, normalized_scope_id = _normalize_scope(scope_type, scope_id)
        normalized_template_id = self._resolve_template_id(
            template_id, export_type=normalized_export_type
        )
        _validate_month(month)
        normalized_currency = _normalize_currency(currency)
        actor_uuid = _actor_identity_uuid(actor_user_id)
        month_lock_status = self._month_lock_status(month)
        normalized_scope_channel_ids = _normalize_scope_channel_ids(
            normalized_scope_type, scope_channel_ids
        )

        row = ExportJobORM(
            id=uuid4(),
            template_id=normalized_template_id,
            export_type=normalized_export_type,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            scope_channel_ids=(
                list(normalized_scope_channel_ids)
                if normalized_scope_channel_ids is not None
                else None
            ),
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

    def get_job(
        self,
        export_id: str,
        *,
        requested_by: str | None = None,
    ) -> ExportJobEntry:
        export_uuid = _parse_uuid(export_id, field_name="export_id")
        row = self._session.scalars(
            select(ExportJobORM).where(
                ExportJobORM.id == export_uuid,
                ExportJobORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None:
            raise ExportJobNotFoundError("Export job not found")
        if requested_by is not None and row.requested_by != _actor_identity_uuid(
            requested_by, field_name="requested_by"
        ):
            raise ExportJobNotFoundError("Export job not found")
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Transition an export job to COMPLETED atomically. Refuses the
    #   write when the row already sits in a terminal status so concurrent
    #   re-generators cannot stomp each other's artifact metadata.
    # Database/ORM: SELECT ... FOR UPDATE on PostgreSQL via SQLAlchemy; the
    #   no-op SQLite SELECT FOR UPDATE keeps test behavior intact.
    # Standards: Race-condition guard per project engineering rules.
    # Blast Radius: Export artifact metadata, audit details.
    # ========================================================================
    def complete_artifact(
        self,
        *,
        export_id: str,
        file_url: str,
        filename: str,
        content_type: str,
        byte_size: int,
        checksum_sha256: str,
    ) -> ExportJobEntry:
        row = self._get_row_for_update(export_id)
        if row.status in _TERMINAL_EXPORT_JOB_STATUSES:
            raise ExportJobTerminalStateError(
                f"Export job {export_id} is already in terminal status {row.status}"
            )
        normalized_file_url = _normalize_artifact_uri(file_url)
        normalized_filename = _normalize_required_string(filename, "artifact_filename")
        normalized_content_type = _normalize_required_string(content_type, "artifact_content_type")
        normalized_byte_size = _normalize_artifact_byte_size(byte_size)
        normalized_checksum = _normalize_sha256(checksum_sha256)
        now = datetime.now(UTC)
        row.status = "COMPLETED"
        row.file_url = normalized_file_url
        row.artifact_filename = normalized_filename
        row.artifact_content_type = normalized_content_type
        row.artifact_byte_size = normalized_byte_size
        row.artifact_checksum_sha256 = normalized_checksum
        row.failure_reason = None
        row.completed_at = now
        row.updated_at = now
        self._session.flush()
        return self._to_entry(row)

    def fail_job(
        self,
        *,
        export_id: str,
        failure_reason: str,
    ) -> ExportJobEntry:
        row = self._get_row_for_update(export_id)
        if row.status in _TERMINAL_EXPORT_JOB_STATUSES:
            raise ExportJobTerminalStateError(
                f"Export job {export_id} is already in terminal status {row.status}"
            )
        normalized_failure_reason = _normalize_required_string(failure_reason, "failure_reason")
        now = datetime.now(UTC)
        row.status = "FAILED"
        row.file_url = None
        row.artifact_filename = None
        row.artifact_content_type = None
        row.artifact_byte_size = None
        row.artifact_checksum_sha256 = None
        row.failure_reason = normalized_failure_reason
        row.completed_at = now
        row.updated_at = now
        self._session.flush()
        return self._to_entry(row)

    def list_jobs(
        self,
        *,
        requested_by: str | None = None,
        visibility_filters: tuple[ExportJobVisibilityFilter, ...] | None = None,
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
                ExportJobORM.requested_by
                == _actor_identity_uuid(requested_by, field_name="requested_by")
            )
        if visibility_filters is not None:
            visibility_conditions = [
                _visibility_filter_condition(visibility_filter)
                for visibility_filter in visibility_filters
                if visibility_filter.export_types
            ]
            if not visibility_conditions:
                return ExportJobPage(items=[], limit=limit, offset=offset, has_more=False)
            statement = statement.where(or_(*visibility_conditions))

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

    def _resolve_template_id(self, template_id: str | None, *, export_type: str) -> UUID | None:
        if template_id is None:
            return None
        template_uuid = _parse_uuid(template_id, field_name="template_id")
        row = self._session.scalars(
            select(ExportTemplateORM).where(
                ExportTemplateORM.id == template_uuid,
                ExportTemplateORM.tenant_id == self._tenant_id,
                ExportTemplateORM.is_active.is_(True),
            )
        ).one_or_none()
        if row is None:
            raise ExportJobValidationError("Export template not found or inactive")
        if row.export_type != export_type:
            raise ExportJobValidationError(
                "Export template export_type must match the requested export_type"
            )
        return template_uuid

    def _get_row(self, export_id: str) -> ExportJobORM:
        export_uuid = _parse_uuid(export_id, field_name="export_id")
        row = self._session.scalars(
            select(ExportJobORM).where(
                ExportJobORM.id == export_uuid,
                ExportJobORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None:
            raise ExportJobNotFoundError("Export job not found")
        return row

    def _get_row_for_update(self, export_id: str) -> ExportJobORM:
        export_uuid = _parse_uuid(export_id, field_name="export_id")
        statement = (
            select(ExportJobORM)
            .where(
                ExportJobORM.id == export_uuid,
                ExportJobORM.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            raise ExportJobNotFoundError("Export job not found")
        return row

    @staticmethod
    def _to_entry(row: ExportJobORM) -> ExportJobEntry:
        stored = row.scope_channel_ids
        scope_channel_ids: tuple[str, ...] | None
        if stored is None:
            scope_channel_ids = None
        else:
            scope_channel_ids = tuple(stored)
        return ExportJobEntry(
            id=str(row.id),
            template_id=str(row.template_id) if row.template_id is not None else None,
            export_type=row.export_type,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            scope_channel_ids=scope_channel_ids,
            month=row.month,
            currency=row.currency,
            requested_by=str(row.requested_by),
            status=row.status,
            file_url=row.file_url,
            artifact_filename=row.artifact_filename,
            artifact_content_type=row.artifact_content_type,
            artifact_byte_size=row.artifact_byte_size,
            artifact_checksum_sha256=row.artifact_checksum_sha256,
            failure_reason=row.failure_reason,
            month_lock_status=row.month_lock_status,
            include_confidence_notes=row.include_confidence_notes,
            include_manual_override_notes=row.include_manual_override_notes,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


def _visibility_filter_condition(visibility_filter: ExportJobVisibilityFilter):
    if visibility_filter.scope_type is None and visibility_filter.scope_id is not None:
        raise ExportJobValidationError(
            "visibility filter must include scope_type when scope_id is set"
        )
    # FIX: SQLAlchemy returns different boolean expression subclasses for IN,
    # equality, and NULL checks; keep the accumulator typed to their shared base.
    conditions: list[ColumnElement[bool]] = [
        ExportJobORM.export_type.in_(sorted(visibility_filter.export_types))
    ]
    if visibility_filter.scope_type is not None:
        conditions.append(ExportJobORM.scope_type == visibility_filter.scope_type)
        if visibility_filter.scope_id is None:
            conditions.append(ExportJobORM.scope_id.is_(None))
        else:
            conditions.append(ExportJobORM.scope_id == visibility_filter.scope_id)
    if visibility_filter.months is not None:
        conditions.append(ExportJobORM.month.in_(sorted(visibility_filter.months)))
    return and_(*conditions)


def is_finance_export_type(export_type: str) -> bool:
    return export_type in FINANCE_EXPORT_TYPES


def _normalize_template_export_type(value: str) -> str:
    try:
        return _normalize_export_type(value)
    except ExportJobValidationError as exc:
        raise ExportTemplateValidationError(str(exc)) from exc


def _normalize_template_name(value: str) -> str:
    try:
        return _normalize_required_string(value, "name")
    except ExportJobValidationError as exc:
        raise ExportTemplateValidationError(str(exc)) from exc


def _normalize_optional_string(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExportTemplateValidationError(f"{field_name} must be a string")
    stripped = value.strip()
    return stripped or None


def _normalize_template_layout_config(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ExportTemplateValidationError("layout_config must be an object")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise ExportTemplateValidationError("layout_config keys must be non-blank strings")
    try:
        return _clone_json_object(value)
    except (TypeError, ValueError) as exc:
        raise ExportTemplateValidationError("layout_config must be JSON serializable") from exc


def _clone_json_object(value: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ExportTemplateValidationError("layout_config must be an object")
    return cast(dict[str, object], decoded)


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
            raise ExportJobValidationError("scope_id must be omitted for global exports")
        return normalized_scope_type, None
    if not normalized_scope_id:
        raise ExportJobValidationError(
            f"scope_id is required for export scope_type: {normalized_scope_type}"
        )
    return normalized_scope_type, normalized_scope_id


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise ExportJobValidationError("month must use YYYY-MM with a calendar month from 01 to 12")


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


def _normalize_artifact_uri(value: str) -> str:
    normalized = _normalize_required_string(value, "file_url")
    if not normalized.startswith(ALLOWED_EXPORT_ARTIFACT_URI_PREFIXES):
        raise ExportJobValidationError("file_url must point to approved artifact storage")
    return normalized


def _normalize_artifact_byte_size(value: int) -> int:
    if value < 0:
        raise ExportJobValidationError("artifact_byte_size must be greater than or equal to 0")
    return value


def _normalize_sha256(value: str) -> str:
    normalized = _normalize_required_string(value, "artifact_checksum_sha256").lower()
    if len(normalized) != 64 or not all(char in "0123456789abcdef" for char in normalized):
        raise ExportJobValidationError("artifact_checksum_sha256 must be a 64-character hex digest")
    return normalized


def _parse_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ExportJobValidationError(f"{field_name} must be a valid UUID") from exc


def _parse_uuid_for_template(value: str, *, field_name: str) -> UUID:
    try:
        return _parse_uuid(value, field_name=field_name)
    except ExportJobValidationError as exc:
        raise ExportTemplateValidationError(str(exc)) from exc


def _actor_identity_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    try:
        return actor_identity_uuid(value, field_name=field_name)
    except ValueError as exc:
        raise ExportJobValidationError(str(exc)) from exc


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or bootstrap."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ExportJobValidationError("tenant_id must be a valid UUID") from exc


def _normalize_scope_channel_ids(
    scope_type: str,
    scope_channel_ids: tuple[str, ...] | frozenset[str] | None,
) -> tuple[str, ...] | None:
    if scope_type == "global":
        if scope_channel_ids is not None:
            raise ExportJobValidationError("scope_channel_ids must be omitted for global exports")
        return None
    if scope_channel_ids is None:
        raise ExportJobValidationError("scope_channel_ids must be provided for non-global exports")
    cleaned: list[str] = []
    for channel_id in scope_channel_ids:
        if not isinstance(channel_id, str):
            raise ExportJobValidationError("scope_channel_ids must contain only strings")
        stripped = channel_id.strip()
        if not stripped:
            raise ExportJobValidationError("scope_channel_ids must not contain blank entries")
        cleaned.append(stripped)
    result = tuple(sorted(set(cleaned)))
    if not result:
        raise ExportJobValidationError("scope_channel_ids must contain at least one channel")
    return result
