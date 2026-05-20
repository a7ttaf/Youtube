from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase

USER_ID = UUID("00000000-0000-0000-0000-000000010101")


def test_raw_report_file_model_persists_metadata_without_file_content():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            RawReportFileORM(
                id=uuid4(),
                source="youtube_reporting",
                report_type="YOUTUBE_CMS_REVENUE",
                report_month="2026-03",
                file_url="s3://ums-raw-reports/youtube/2026-03/cms.csv",
                checksum="sha256:83f8b7d92d8a",
                parse_status="DOWNLOADED",
                downloaded_by=USER_ID,
            )
        )
        session.commit()
        raw_file = session.scalars(select(RawReportFileORM)).one()

    assert raw_file.source == "youtube_reporting"
    assert raw_file.report_type == "YOUTUBE_CMS_REVENUE"
    assert raw_file.file_url.startswith("s3://")
    assert not hasattr(raw_file, "file_content")
