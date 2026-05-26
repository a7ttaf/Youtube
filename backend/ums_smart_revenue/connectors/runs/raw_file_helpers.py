"""raw_report_files lifecycle helpers used by the B2.4 orchestrator.

Allowed transitions (spec §5.2):
- DOWNLOADED -> PARSED  via mark_parsed (success)
- FAILED     -> PARSED  via mark_parsed (retry recovery)
- DOWNLOADED -> FAILED  via mark_failed
- FAILED     -> FAILED  via mark_failed (idempotent: overwrites error fields)

Refused (raise):
- QUARANTINED -> anything (terminal; externally-set)
- PARSED -> PARSED via mark_parsed (RawFileAlreadyParsedError)
- PARSED -> FAILED, DOWNLOADED -> DOWNLOADED, any other (RawFileLifecycleError)

Tenant scope is enforced: a (raw_file_id, tenant_id) mismatch is a
RawFileLifecycleError, not a silent no-op.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    RawFileAlreadyParsedError,
    RawFileLifecycleError,
)
from ums_smart_revenue.db.report_models import RawReportFileORM

_ERROR_SUMMARY_MAX = 500


def _load_or_raise(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID, target: str
) -> RawReportFileORM:
    stmt = select(RawReportFileORM).where(
        RawReportFileORM.id == raw_file_id,
        RawReportFileORM.tenant_id == tenant_id,
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise RawFileLifecycleError(
            raw_file_id=str(raw_file_id), current="<missing>", target=target
        )
    return row


def mark_parsed(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID
) -> None:
    row = _load_or_raise(
        session, raw_file_id=raw_file_id, tenant_id=tenant_id, target="PARSED"
    )
    if row.parse_status == "PARSED":
        raise RawFileAlreadyParsedError(raw_file_id=str(raw_file_id))
    if row.parse_status in ("DOWNLOADED", "FAILED"):
        row.parse_status = "PARSED"
        return
    raise RawFileLifecycleError(
        raw_file_id=str(raw_file_id),
        current=row.parse_status,
        target="PARSED",
    )
