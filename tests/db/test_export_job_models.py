from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.report_models import ExportJobORM, ReportBase


def test_export_job_model_persists_queue_metadata():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    requested_by = uuid4()

    with Session(engine) as session:
        session.add(
            ExportJobORM(
                id=uuid4(),
                export_type="FINANCE_EXCEL",
                scope_type="company",
                scope_id="company-tv-a",
                month="2026-03",
                currency="USD",
                requested_by=requested_by,
                status="QUEUED",
                month_lock_status="LOCKED",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        session.commit()
        export_job = session.scalars(select(ExportJobORM)).one()

    assert export_job.export_type == "FINANCE_EXCEL"
    assert export_job.scope_type == "company"
    assert export_job.requested_by == requested_by
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert export_job.artifact_filename is None
    assert export_job.artifact_content_type is None
    assert export_job.artifact_byte_size is None
    assert export_job.artifact_checksum_sha256 is None
    assert export_job.failure_reason is None
    assert export_job.month_lock_status == "LOCKED"
    assert export_job.include_confidence_notes is True
    assert export_job.include_manual_override_notes is True
