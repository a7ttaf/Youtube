import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PURGED_PARSE_STATUS = "PURGED"
ALLOWED_PARSE_STATUSES = frozenset({"DOWNLOADED", "PARSED", "FAILED", "QUARANTINED"})
ALLOWED_STORAGE_PREFIXES = ("s3://", "gs://", "azure://", "blob://", "file-store://")
MAX_RAW_REPORT_FILE_PAGE_SIZE = 100
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class _RowCountResult(Protocol):
    rowcount: int


@dataclass(frozen=True)
class RawReportFileEntry:
    id: str
    source: str
    report_type: str
    report_month: str
    storage_uri: str
    checksum: str
    parse_status: str
    downloaded_by: str | None
    downloaded_at: datetime
    purged_by: str | None = None
    purged_at: datetime | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "report_type": self.report_type,
            "report_month": self.report_month,
            "storage_uri": self.storage_uri,
            "checksum": self.checksum,
            "parse_status": self.parse_status,
            "downloaded_by": self.downloaded_by,
            "downloaded_at": self.downloaded_at.isoformat(),
        }


@dataclass(frozen=True)
class RawReportFilePage:
    items: list[RawReportFileEntry]
    limit: int
    offset: int
    has_more: bool


class RawReportFileError(ValueError):
    pass


class RawReportFileConflictError(RawReportFileError):
    pass


class RawReportFileNotFoundError(RawReportFileError):
    pass


class RawReportFileValidationError(RawReportFileError):
    pass


class RawReportFilePurgeConflictError(RawReportFileError):
    pass


class SqlAlchemyRawReportFileRepository:
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind raw report file metadata operations to one tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def register_file(
        self,
        *,
        source: str,
        report_type: str,
        report_month: str,
        storage_uri: str,
        checksum: str,
        parse_status: str,
        actor_user_id: str,
    ) -> RawReportFileEntry:
        normalized_source = _normalize_required_string(source, "source")
        normalized_report_type = _normalize_required_string(report_type, "report_type")
        _validate_month(report_month)
        normalized_storage_uri = _normalize_storage_uri(storage_uri)
        normalized_checksum = _normalize_required_string(checksum, "checksum")
        normalized_parse_status = _normalize_parse_status(parse_status)
        actor_uuid = _actor_identity_uuid(actor_user_id)

        row = RawReportFileORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            source=normalized_source,
            report_type=normalized_report_type,
            report_month=report_month,
            file_url=normalized_storage_uri,
            checksum=normalized_checksum,
            parse_status=normalized_parse_status,
            downloaded_by=actor_uuid,
            updated_at=datetime.now(UTC),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            if _is_duplicate_raw_report_file_integrity_error(exc):
                raise RawReportFileConflictError("Raw report file metadata already exists") from exc
            raise
        return self._to_entry(row)

    def get_file(self, raw_file_id: str) -> RawReportFileEntry:
        row = self._get_row(raw_file_id)
        return self._to_entry(row)

    def list_files(
        self,
        *,
        source: str | None = None,
        report_type: str | None = None,
        report_month: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RawReportFilePage:
        if limit < 1 or limit > MAX_RAW_REPORT_FILE_PAGE_SIZE:
            raise RawReportFileValidationError(
                f"limit must be between 1 and {MAX_RAW_REPORT_FILE_PAGE_SIZE}"
            )
        if offset < 0:
            raise RawReportFileValidationError("offset must be greater than or equal to 0")
        if report_month is not None:
            _validate_month(report_month)

        statement = (
            select(RawReportFileORM)
            .where(RawReportFileORM.tenant_id == self._tenant_id)
            .order_by(
                RawReportFileORM.downloaded_at.desc(),
                RawReportFileORM.id.desc(),
            )
        )
        if source is not None:
            statement = statement.where(
                RawReportFileORM.source == _normalize_required_string(source, "source")
            )
        if report_type is not None:
            statement = statement.where(
                RawReportFileORM.report_type
                == _normalize_required_string(report_type, "report_type")
            )
        if report_month is not None:
            statement = statement.where(RawReportFileORM.report_month == report_month)

        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
        return RawReportFilePage(
            items=[self._to_entry(row) for row in rows[:limit]],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def _get_row(self, raw_file_id: str) -> RawReportFileORM:
        raw_file_uuid = _parse_uuid(raw_file_id, field_name="raw_report_file_id")
        row = self._session.scalars(
            select(RawReportFileORM).where(
                RawReportFileORM.tenant_id == self._tenant_id,
                RawReportFileORM.id == raw_file_uuid,
            )
        ).one_or_none()
        if row is None:
            raise RawReportFileNotFoundError("Raw report file not found")
        return row

    # ========================================================================
    # Purpose: Mark one raw report file PURGED on explicit authorized request;
    #   clear the storage pointer while keeping all metadata for the audit
    #   trail. Default policy is retain-forever, so this is the only delete path.
    # Database/ORM: RawReportFileORM (raw_report_files); tenant-scoped UPDATE.
    # Standards: Typed domain errors; fail-closed on cross-tenant/unknown ids.
    # Blast Radius: Audit (caller emits REPORT_PURGED); no finance/Neo4j impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/reports.py -> DELETE route caller.
    # ========================================================================
    def purge_file(
        self, *, raw_file_id: str, actor_user_id: str, reason: str
    ) -> RawReportFileEntry:
        """Set parse_status=PURGED, clear file_url, stamp purged_at/purged_by."""
        if not reason or not reason.strip():
            raise RawReportFileValidationError("reason must not be blank")
        raw_file_uuid = _parse_uuid(raw_file_id, field_name="raw_report_file_id")
        actor_uuid = _actor_identity_uuid(actor_user_id)
        now = datetime.now(UTC)
        result = cast(
            _RowCountResult,
            self._session.execute(
                update(RawReportFileORM)
                .where(
                    RawReportFileORM.tenant_id == self._tenant_id,
                    RawReportFileORM.id == raw_file_uuid,
                    RawReportFileORM.parse_status != PURGED_PARSE_STATUS,
                )
                .values(
                    parse_status=PURGED_PARSE_STATUS,
                    file_url="",
                    purged_by=actor_uuid,
                    purged_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        self._session.flush()
        if result.rowcount != 1:
            # FIX: DELETE routes preload this row for authorization; force this
            # reread to bypass the identity map so concurrent PURGED state maps
            # to the documented 409 instead of a stale generic conflict.
            row = self._session.scalars(
                select(RawReportFileORM)
                .where(
                    RawReportFileORM.tenant_id == self._tenant_id,
                    RawReportFileORM.id == raw_file_uuid,
                )
                .execution_options(populate_existing=True)
            ).one_or_none()
            if row is None:
                raise RawReportFileNotFoundError("Raw report file not found")
            if row.parse_status == PURGED_PARSE_STATUS:
                raise RawReportFilePurgeConflictError("Raw report file is already purged")
            raise RawReportFileConflictError("Raw report file purge was not applied")
        row = self._session.scalars(
            select(RawReportFileORM)
            .where(
                RawReportFileORM.tenant_id == self._tenant_id,
                RawReportFileORM.id == raw_file_uuid,
            )
            .execution_options(populate_existing=True)
        ).one()
        return self._to_entry(row)

    @staticmethod
    def _to_entry(row: RawReportFileORM) -> RawReportFileEntry:
        return RawReportFileEntry(
            id=str(row.id),
            source=row.source,
            report_type=row.report_type,
            report_month=row.report_month,
            storage_uri=row.file_url,
            checksum=row.checksum,
            parse_status=row.parse_status,
            downloaded_by=str(row.downloaded_by) if row.downloaded_by else None,
            downloaded_at=_ensure_utc(row.downloaded_at),
            purged_by=str(row.purged_by) if row.purged_by else None,
            purged_at=_ensure_utc(row.purged_at) if row.purged_at else None,
        )


# FIX: SQLite drops timezone metadata for DateTime(timezone=True); repository
# entries keep the same UTC-aware contract that Postgres timestamptz returns.
def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise RawReportFileValidationError(
            "report_month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RawReportFileValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_storage_uri(value: str) -> str:
    normalized = _normalize_required_string(value, "storage_uri")
    if not normalized.startswith(ALLOWED_STORAGE_PREFIXES):
        raise RawReportFileValidationError(
            "storage_uri must point to approved object storage, not raw inline report content"
        )
    return normalized


def _normalize_parse_status(value: str) -> str:
    normalized = _normalize_required_string(value, "parse_status")
    if normalized not in ALLOWED_PARSE_STATUSES:
        if normalized == PURGED_PARSE_STATUS:
            raise RawReportFileValidationError(
                "parse_status PURGED is only allowed through the purge endpoint"
            )
        raise RawReportFileValidationError(f"Unknown raw report parse_status: {value}")
    return normalized


def _actor_identity_uuid(value: str) -> UUID:
    # Accept either a UUID literal or a trusted-gateway subject; the shared
    # helper derives a deterministic UUID5 for the latter so header-auth
    # deployments with non-UUID x-user-id values can still register raw
    # report files via connector ingestion. Entity IDs (raw_report_file_id)
    # still use the strict _parse_uuid below since they must reference a
    # real DB row.
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise RawReportFileValidationError(str(exc)) from exc


def _parse_uuid(value: str, *, field_name: str = "raw_report_file_id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise RawReportFileValidationError(f"{field_name} must be a valid UUID") from exc


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
        raise RawReportFileValidationError("tenant_id must be a valid UUID") from exc


def _is_duplicate_raw_report_file_integrity_error(error: IntegrityError) -> bool:
    orig = getattr(error, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name == "uq_raw_report_files_source_type_month_checksum":
        return True
    message = str(orig)
    return "raw_report_files.source" in message and "raw_report_files.report_type" in message
