from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.report_models import ExportJobORM, ExportTemplateORM, ReportBase
from ums_smart_revenue.reports.exports import (
    ExportJobNotFoundError,
    ExportJobValidationError,
    ExportTemplateValidationError,
    SqlAlchemyExportJobRepository,
    SqlAlchemyExportTemplateRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
OTHER_TENANT_UUID = UUID("00000000-0000-0000-0000-000000096999")
ACTOR_USER_ID = "00000000-0000-0000-0000-000000096001"


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def tenant(tenant_id: UUID = OTHER_TENANT_UUID) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug="other",
        display_name="Other Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def request_default_export(repo: SqlAlchemyExportJobRepository):
    return repo.request_export(
        export_type="FINANCE_EXCEL",
        scope_type="global",
        scope_id=None,
        month="2026-03",
        currency="USD",
        actor_user_id=ACTOR_USER_ID,
        include_confidence_notes=True,
        include_manual_override_notes=True,
    )


def test_request_export_uses_ambient_tenant_context():
    with build_session() as session:
        token = TENANT_CTX.set(tenant())
        try:
            repo = SqlAlchemyExportJobRepository(session)
            request_default_export(repo)
            session.commit()
        finally:
            TENANT_CTX.reset(token)

        row = session.scalars(select(ExportJobORM)).one()
        assert row.tenant_id == OTHER_TENANT_UUID


def test_template_create_translates_duplicate_name_race(monkeypatch):
    with build_session() as session:
        repo = SqlAlchemyExportTemplateRepository(session)
        repo.create_template(
            name="Finance workbook standard",
            export_type="FINANCE_EXCEL",
            layout_config={"sheets": ["summary"]},
            actor_user_id=ACTOR_USER_ID,
        )
        session.commit()

        racing_repo = SqlAlchemyExportTemplateRepository(session)
        monkeypatch.setattr(
            racing_repo,
            "_ensure_template_name_available",
            lambda *args, **kwargs: None,
        )

        with pytest.raises(ExportTemplateValidationError, match="already exists"):
            racing_repo.create_template(
                name="Finance workbook standard",
                export_type="FINANCE_EXCEL",
                layout_config={"sheets": ["payments"]},
                actor_user_id=ACTOR_USER_ID,
            )

        session.rollback()
        template_names = session.scalars(select(ExportTemplateORM.name)).all()

    assert template_names == ["Finance workbook standard"]


def test_request_export_rejects_inactive_template():
    """An export request must reject an inactive template under the active-selection lock.

    Covers the deactivation-vs-selection race: ``_resolve_template_id`` reads the
    template with ``FOR UPDATE`` and requires ``is_active`` at selection time, so
    a template that has been retired cannot be referenced by a new export job.
    SQLite's SELECT FOR UPDATE is a no-op, so this asserts the active-filter +
    validation contract that the lock serializes on Postgres.
    """
    with build_session() as session:
        repo = SqlAlchemyExportJobRepository(session)
        template_repo = SqlAlchemyExportTemplateRepository(session)
        template = template_repo.create_template(
            name="Finance workbook standard",
            export_type="FINANCE_EXCEL",
            layout_config={"sheets": ["summary"]},
            actor_user_id=ACTOR_USER_ID,
        )
        template_repo.deactivate_template(template.id)
        session.commit()

        with pytest.raises(ExportJobValidationError, match="not found or inactive"):
            repo.request_export(
                export_type="FINANCE_EXCEL",
                scope_type="global",
                scope_id=None,
                month="2026-03",
                currency="USD",
                actor_user_id=ACTOR_USER_ID,
                include_confidence_notes=True,
                include_manual_override_notes=True,
                template_id=template.id,
            )


def test_request_export_rejects_mismatched_template_export_type():
    """An export request must reject a template whose export_type differs from the request."""
    with build_session() as session:
        repo = SqlAlchemyExportJobRepository(session)
        template_repo = SqlAlchemyExportTemplateRepository(session)
        template = template_repo.create_template(
            name="Analytics standard",
            export_type="ANALYTICS_SUMMARY_CSV",
            layout_config={"columns": ["channel"]},
            actor_user_id=ACTOR_USER_ID,
        )
        session.commit()

        with pytest.raises(ExportJobValidationError, match="export_type must match"):
            repo.request_export(
                export_type="FINANCE_EXCEL",
                scope_type="global",
                scope_id=None,
                month="2026-03",
                currency="USD",
                actor_user_id=ACTOR_USER_ID,
                include_confidence_notes=True,
                include_manual_override_notes=True,
                template_id=template.id,
            )


def test_complete_artifact_does_not_update_other_tenants_job():
    with build_session() as session:
        foreign_repo = SqlAlchemyExportJobRepository(session, tenant_id=OTHER_TENANT_UUID)
        foreign = request_default_export(foreign_repo)
        session.commit()

        default_repo = SqlAlchemyExportJobRepository(session)
        with pytest.raises(ExportJobNotFoundError, match="Export job not found"):
            default_repo.complete_artifact(
                export_id=foreign.id,
                file_url="s3://exports/finance.xlsx",
                filename="finance.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                byte_size=128,
                checksum_sha256="a" * 64,
            )


def test_repository_rejects_malformed_tenant_id():
    with (
        build_session() as session,
        pytest.raises(ExportJobValidationError, match="tenant_id must be a valid UUID"),
    ):
        SqlAlchemyExportJobRepository(session, tenant_id="not-a-uuid")
