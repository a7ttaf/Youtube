from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.reports.raw_files import (
    RawReportFileNotFoundError,
    RawReportFilePurgeConflictError,
    SqlAlchemyRawReportFileRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
ACTOR_USER_ID = "00000000-0000-0000-0000-000000071001"
SECOND_ACTOR_USER_ID = "00000000-0000-0000-0000-000000071002"


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
    repo.purge_file(raw_file_id=registered.id, actor_user_id=ACTOR_USER_ID, reason="first")

    with pytest.raises(RawReportFilePurgeConflictError):
        repo.purge_file(raw_file_id=registered.id, actor_user_id=ACTOR_USER_ID, reason="second")


def test_purge_status_transition_is_conditional_under_stale_session(tmp_path):
    """A stale session must not overwrite another transaction's PURGED stamp."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'raw-files.db'}")
    ReportBase.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as first_session:
        first_repo = SqlAlchemyRawReportFileRepository(first_session, tenant_id=DEFAULT_TENANT_UUID)
        registered = register_default(first_repo)
        first_session.commit()
        first_session.scalars(
            select(RawReportFileORM).where(RawReportFileORM.id == UUID(registered.id))
        ).one()
        first_session.commit()

        with Session(engine) as second_session:
            second_repo = SqlAlchemyRawReportFileRepository(
                second_session, tenant_id=DEFAULT_TENANT_UUID
            )
            second_repo.purge_file(
                raw_file_id=registered.id,
                actor_user_id=SECOND_ACTOR_USER_ID,
                reason="first purge wins",
            )
            second_session.commit()

        with pytest.raises(RawReportFilePurgeConflictError):
            first_repo.purge_file(
                raw_file_id=registered.id,
                actor_user_id=ACTOR_USER_ID,
                reason="stale retry",
            )

    with Session(engine) as check_session:
        row = check_session.scalars(
            select(RawReportFileORM).where(RawReportFileORM.id == UUID(registered.id))
        ).one()
        assert row.purged_by == UUID(SECOND_ACTOR_USER_ID)


def test_purge_unknown_id_raises_not_found():
    session = build_session()
    repo = SqlAlchemyRawReportFileRepository(session, tenant_id=DEFAULT_TENANT_UUID)

    with pytest.raises(RawReportFileNotFoundError):
        repo.purge_file(
            raw_file_id=str(uuid4()),
            actor_user_id=ACTOR_USER_ID,
            reason="missing",
        )
