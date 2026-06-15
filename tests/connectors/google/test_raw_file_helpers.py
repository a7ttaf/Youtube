"""Raw file lifecycle helper tests.

mark_parsed: DOWNLOADED|FAILED -> PARSED. Refuses QUARANTINED.
RawFileAlreadyParsedError on PARSED -> PARSED. RawFileLifecycleError
otherwise (illegal transition).

mark_failed: DOWNLOADED|FAILED -> FAILED (idempotent on FAILED, no raise).
Refuses QUARANTINED and PARSED. Does NOT write error_class/error_summary
to raw_report_files (no such columns per PR #32 schema; spec §3 non-goal).
Error context flows to connector_runs.error_summary (B2.3 finish_run)
and the REPORT_IMPORTED audit payload (B2.6, error_class only).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    RawFileAlreadyParsedError,
    RawFileLifecycleError,
)
from ums_smart_revenue.connectors.runs.raw_file_helpers import (
    mark_failed,
    mark_parsed,
)
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _insert_raw_file(session: Session, *, tenant_id: UUID, parse_status: str) -> UUID:
    # SQLite does not implement gen_random_uuid(); the production
    # server_default is Postgres-only, so tests supply the id explicitly.
    row = RawReportFileORM(
        id=uuid4(),
        tenant_id=tenant_id,
        source="youtube_reporting",
        report_type="channel_basic_a2",
        report_month="2026-05",
        file_url="file-store://x/y.csv",
        checksum="abc",
        parse_status=parse_status,
    )
    session.add(row)
    session.flush()
    return row.id


def test_mark_parsed_downloaded_to_parsed(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="DOWNLOADED")
    mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    session.flush()
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "PARSED"


def test_mark_parsed_failed_to_parsed_retry_recovery(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="FAILED")
    mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "PARSED"


def test_mark_parsed_already_parsed_raises(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="PARSED")
    with pytest.raises(RawFileAlreadyParsedError):
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)


def test_mark_parsed_quarantined_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="QUARANTINED")
    with pytest.raises(RawFileLifecycleError) as ctx:
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    assert ctx.value.current == "QUARANTINED"
    assert ctx.value.target == "PARSED"


def test_mark_parsed_unknown_row_raises_lifecycle(session) -> None:
    tenant_id = uuid4()
    with pytest.raises(RawFileLifecycleError):
        mark_parsed(session, raw_file_id=uuid4(), tenant_id=tenant_id)


def test_mark_parsed_cross_tenant_refused(session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_a, parse_status="DOWNLOADED")
    with pytest.raises(RawFileLifecycleError):
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_b)


def test_mark_failed_downloaded_to_failed(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="DOWNLOADED")
    mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "FAILED"


def test_mark_failed_failed_to_failed_idempotent(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="FAILED")
    mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "FAILED"  # unchanged but no raise


def test_mark_failed_parsed_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="PARSED")
    with pytest.raises(RawFileLifecycleError):
        mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)


def test_mark_failed_quarantined_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="QUARANTINED")
    with pytest.raises(RawFileLifecycleError):
        mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
