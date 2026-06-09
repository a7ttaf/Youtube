from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.reports.raw_files import (
    RawReportFileNotFoundError,
    RawReportFilePurgeConflictError,
    SqlAlchemyRawReportFileRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
ACTOR_USER_ID = "00000000-0000-0000-0000-000000071001"


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    return Session(engine)


def register_default(repo: SqlAlchemyRawReportFileRepository):
    return repo.register_file(
        source="youtube_reporting",
        report_type="YOUTUBE_CMS_REVENUE",
        report_month="2026-03",
        storage_uri="s3://ums-raw-reports/youtube/2026-03/cms.csv",
        checksum="sha256:83f8b7d92d8a",
        parse_status="DOWNLOADED",
        actor_user_id=ACTOR_USER_ID,
    )


def test_purge_marks_purged_clears_url_keeps_metadata():
    session = build_session()
    repo = SqlAlchemyRawReportFileRepository(session, tenant_id=DEFAULT_TENANT_UUID)
    registered = register_default(repo)

    before = datetime.now(UTC)
    purged = repo.purge_file(
        raw_file_id=registered.id,
        actor_user_id=ACTOR_USER_ID,
        reason="Operator-requested deletion",
    )

    assert purged.parse_status == "PURGED"
    assert purged.storage_uri == ""
    assert purged.purged_by is not None
    assert purged.purged_at is not None
    assert purged.purged_at >= before
    # Metadata kept for the audit trail.
    assert purged.source == "youtube_reporting"
    assert purged.report_type == "YOUTUBE_CMS_REVENUE"
    assert purged.report_month == "2026-03"
    assert purged.checksum == "sha256:83f8b7d92d8a"


def test_repurge_raises_conflict():
    session = build_session()
    repo = SqlAlchemyRawReportFileRepository(session, tenant_id=DEFAULT_TENANT_UUID)
    registered = register_default(repo)
    repo.purge_file(
        raw_file_id=registered.id, actor_user_id=ACTOR_USER_ID, reason="first"
    )

    with pytest.raises(RawReportFilePurgeConflictError):
        repo.purge_file(
            raw_file_id=registered.id, actor_user_id=ACTOR_USER_ID, reason="second"
        )


def test_purge_unknown_id_raises_not_found():
    session = build_session()
    repo = SqlAlchemyRawReportFileRepository(session, tenant_id=DEFAULT_TENANT_UUID)

    with pytest.raises(RawReportFileNotFoundError):
        repo.purge_file(
            raw_file_id=str(uuid4()),
            actor_user_id=ACTOR_USER_ID,
            reason="missing",
        )
