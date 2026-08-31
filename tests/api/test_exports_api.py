# ============================================================================
# Purpose: Validate export job and export-template API behavior.
# Database/ORM: SQLite test database with finance, report, org, and audit ORM.
# Standards: Route-level behavior checks with real app wiring and audit asserts.
# Blast Radius: Export API contracts, finance lock snapshots, and template CRUD.
# Connections:
#   - File: backend/ums_smart_revenue/api/exports.py -> Export job API.
#   - File: backend/ums_smart_revenue/api/export_templates.py -> Template API.
# ============================================================================
import csv
import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.api.dependencies_audit import current_audit_sink
from ums_smart_revenue.api.exports import (
    _list_authorized_export_jobs,
    current_export_artifact_store,
)
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.report_models import ExportJobORM, ExportTemplateORM, ReportBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.source_models import CurrencyORM, GoogleRevenueSourceRowORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.reports.artifact_storage import FileSystemExportArtifactStore
from ums_smart_revenue.reports.exports import ExportJobEntry, ExportJobPage
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

SECTOR_ID = UUID("00000000-0000-0000-0000-000000012001")
COMPANY_A_ID = UUID("00000000-0000-0000-0000-000000012101")
COMPANY_B_ID = UUID("00000000-0000-0000-0000-000000012102")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-000000012201")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-000000012202")
GROUP_ID = UUID("00000000-0000-0000-0000-000000012301")
USER_ID = UUID("00000000-0000-0000-0000-000000012401")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    user_id: str | UUID = USER_ID,
) -> dict[str, str]:
    """Build trusted-gateway headers for export API permission scenarios."""
    headers = {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """Return an isolated SQLite database URL for export API tests."""
    return f"sqlite+pysqlite:///{(tmp_path / 'exports.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed export tests with authorization, org, finance, and report rows."""
    engine = create_engine(database_url)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                TenantORM(id=UUID(UMS_TENANT_ID), slug="ums", display_name="UMS"),
                CurrencyORM(
                    code="USD",
                    numeric_code="840",
                    name="US Dollar",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                CurrencyORM(
                    code="EUR",
                    numeric_code="978",
                    name="Euro",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                UserORM(id=USER_ID, email="exports@example.com", display_name="Exports User"),
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(
                    id=COMPANY_A_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="Company A",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_B_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="Company B",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_A_ROW_ID,
                    youtube_channel_id="channel-a",
                    channel_name="Channel A",
                    primary_org_unit_id=COMPANY_A_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_B_ROW_ID,
                    youtube_channel_id="channel-b",
                    channel_name="Channel B",
                    primary_org_unit_id=COMPANY_B_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                ChannelGroupORM(
                    id=GROUP_ID, name="Mixed Group", group_type="CUSTOM_GROUP", active=True
                ),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_A_ROW_ID),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_B_ROW_ID),
                FinanceMonthCloseORM(month="2026-03", status="LOCKED", allocation_rule_payload={}),
            ]
        )
        session.commit()


def _analytics_source_row(
    *,
    channel_id: str,
    amount: Decimal,
    metric_key: str = "estimatedRevenue",
    value_kind: str = "estimated",
    currency_code: str = "USD",
    month: str = "2026-03",
    source_system: str = "youtube_analytics",
    ingested_at: datetime | None = None,
) -> GoogleRevenueSourceRowORM:
    """Build one YouTube Analytics source row for export download tests."""
    row_id = uuid4()
    return GoogleRevenueSourceRowORM(
        id=row_id,
        tenant_id=UUID(UMS_TENANT_ID),
        source_system=source_system,
        source_row_key=row_id.hex.ljust(64, "0"),
        source_account_id="analytics-account-secret",
        content_owner_id="content-owner-secret",
        youtube_channel_id=channel_id,
        report_type="reports.query",
        report_month=month,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        metric_key=metric_key,
        value_kind=value_kind,
        amount_native=amount,
        currency_code=currency_code,
        source_report_id="source-report-secret",
        raw_file_id=None,
        raw_payload={"secret": "must-not-leak"},
        imported_by=None,
        ingested_at=ingested_at or datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
    )


def _seed_analytics_csv_export_job(
    database_url: str,
    *,
    export_id: UUID | None = None,
    scope_type: str = "company",
    scope_id: str | None = str(COMPANY_A_ID),
    scope_channel_ids: tuple[str, ...] | None = ("channel-a",),
    currency: str = "USD",
    requested_by: UUID = USER_ID,
    job_status: str = "QUEUED",
) -> UUID:
    """Persist one queued analytics CSV export job for download-gate tests."""
    export_uuid = export_id or uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ExportJobORM(
                id=export_uuid,
                export_type="ANALYTICS_SUMMARY_CSV",
                scope_type=scope_type,
                scope_id=scope_id,
                scope_channel_ids=(
                    list(scope_channel_ids) if scope_channel_ids is not None else None
                ),
                month="2026-03",
                currency=currency,
                requested_by=requested_by,
                status=job_status,
                month_lock_status="LOCKED",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        session.commit()
    return export_uuid


def test_finance_admin_requests_finance_export_with_audit_and_lock_snapshot(tmp_path):
    """Verify finance export requests persist audit and lock snapshot metadata."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "include_confidence_notes": True,
            "include_manual_override_notes": True,
            "reason": "Monthly finance close workbook",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_job = session.scalars(select(ExportJobORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 202
    assert response.json()["export_type"] == "FINANCE_EXCEL"
    assert response.json()["status"] == "QUEUED"
    assert response.json()["file_url"] is None
    assert response.json()["month_lock_status"] == "LOCKED"
    assert response.json()["audit_event"]["event_type"] == "EXPORT_CREATED"
    assert response.json()["scope_channel_ids"] == ["channel-a"]
    assert export_job.scope_id == str(COMPANY_A_ID)
    assert export_job.scope_channel_ids == ["channel-a"]
    assert export_job.month_lock_status == "LOCKED"
    assert audit_log.event_type == "EXPORT_CREATED"
    assert audit_log.sensitive is True


def test_corporate_admin_manages_export_template_lifecycle_with_audit(tmp_path):
    """Verify template CRUD is audited and soft-deletes active list visibility."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("corporate_admin")

    create_response = client.post(
        "/export-templates",
        headers=headers,
        json={
            "name": "Finance workbook standard",
            "export_type": "FINANCE_EXCEL",
            "description": "Monthly finance workbook layout",
            "layout_config": {"sheets": ["summary", "payments"], "version": 1},
            "reason": "Create reusable finance layout",
        },
    )
    template_id = create_response.json()["id"]

    list_response = client.get(
        "/export-templates?export_type=FINANCE_EXCEL",
        headers=headers,
    )
    update_response = client.patch(
        f"/export-templates/{template_id}",
        headers=headers,
        json={
            "name": "Finance workbook board pack",
            "description": None,
            "layout_config": {"sheets": ["summary", "alerts"], "version": 2},
            "reason": "Update workbook layout",
        },
    )
    delete_response = client.delete(
        f"/export-templates/{template_id}?reason=Retire%20template",
        headers=headers,
    )
    active_list_response = client.get("/export-templates", headers=headers)
    inactive_list_response = client.get(
        "/export-templates?include_inactive=true",
        headers=headers,
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        template = session.scalars(select(ExportTemplateORM)).one()
        audit_events = session.scalars(select(AuditLogORM)).all()

    assert create_response.status_code == 201
    assert create_response.json()["name"] == "Finance workbook standard"
    assert create_response.json()["is_active"] is True
    assert create_response.json()["audit_event"]["event_type"] == "EXPORT_TEMPLATE_CHANGED"
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["items"]] == [template_id]
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Finance workbook board pack"
    assert update_response.json()["description"] is None
    assert update_response.json()["layout_config"] == {
        "sheets": ["summary", "alerts"],
        "version": 2,
    }
    assert delete_response.status_code == 200
    assert delete_response.json()["is_active"] is False
    assert active_list_response.json()["items"] == []
    assert [item["id"] for item in inactive_list_response.json()["items"]] == [template_id]
    assert template.name == "Finance workbook board pack"
    assert template.is_active is False
    assert [event.event_type for event in audit_events] == [
        "EXPORT_TEMPLATE_CHANGED",
        "EXPORT_TEMPLATE_CHANGED",
        "EXPORT_TEMPLATE_CHANGED",
    ]
    assert all(event.sensitive is True for event in audit_events)


def test_export_template_update_rejects_null_only_noop(tmp_path):
    """Verify nullable PATCH fields cannot create a fake update audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("corporate_admin")

    create_response = client.post(
        "/export-templates",
        headers=headers,
        json={
            "name": "Finance workbook standard",
            "export_type": "FINANCE_EXCEL",
            "layout_config": {"sheets": ["summary"]},
            "reason": "Create reusable finance layout",
        },
    )
    template_id = create_response.json()["id"]
    response = client.patch(
        f"/export-templates/{template_id}",
        headers=headers,
        json={"name": None, "is_active": None, "reason": "No effective update"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        template = session.scalars(select(ExportTemplateORM)).one()
        audit_events = session.scalars(select(AuditLogORM)).all()

    assert create_response.status_code == 201
    assert response.status_code == 422
    assert template.name == "Finance workbook standard"
    assert [event.event_type for event in audit_events] == ["EXPORT_TEMPLATE_CHANGED"]


def test_export_template_create_rejects_excessively_nested_layout(tmp_path):
    """Verify layout_config is bounded before it reaches storage."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    layout_config: dict[str, object] = {"level": {}}
    nested = layout_config["level"]
    assert isinstance(nested, dict)
    for index in range(10):
        nested[f"level_{index}"] = {}
        nested = nested[f"level_{index}"]

    response = client.post(
        "/export-templates",
        headers=auth_headers("corporate_admin"),
        json={
            "name": "Overly nested workbook layout",
            "export_type": "FINANCE_EXCEL",
            "layout_config": layout_config,
            "reason": "Validate layout guard",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        template_count = session.scalar(select(func.count()).select_from(ExportTemplateORM))
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 422
    assert "layout_config nesting" in response.json()["detail"]
    assert template_count == 0
    assert audit_count == 0


def test_export_template_management_requires_permission(tmp_path):
    """Verify export template writes fail closed without template management grants."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/export-templates",
        headers=auth_headers("assistant_analyst"),
        json={
            "name": "Analyst template probe",
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "layout_config": {"columns": ["report_month"]},
            "reason": "Probe template access",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        template_count = session.scalar(select(func.count()).select_from(ExportTemplateORM))
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.manage_templates"
    assert template_count == 0
    assert audit_count == 0


def test_finance_export_request_persists_active_template_selection(tmp_path):
    """Verify export job creation stores the selected active matching template."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    template_response = client.post(
        "/export-templates",
        headers=auth_headers("corporate_admin"),
        json={
            "name": "Finance workbook standard",
            "export_type": "FINANCE_EXCEL",
            "layout_config": {"sheets": ["summary", "payments"]},
            "reason": "Create reusable finance layout",
        },
    )
    template_id = template_response.json()["id"]
    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "template_id": template_id,
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Monthly finance close workbook",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_job = session.scalars(select(ExportJobORM)).one()
        audit_events = session.scalars(select(AuditLogORM)).all()

    export_created = [event for event in audit_events if event.event_type == "EXPORT_CREATED"]
    assert template_response.status_code == 201
    assert response.status_code == 202
    assert response.json()["template_id"] == template_id
    assert export_job.template_id == UUID(template_id)
    assert len(export_created) == 1
    assert export_created[0].details["template_id"] == template_id


def test_export_request_rejects_mismatched_export_template(tmp_path):
    """Verify template export_type must match the requested export job type."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    template_response = client.post(
        "/export-templates",
        headers=auth_headers("corporate_admin"),
        json={
            "name": "Analytics CSV standard",
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "layout_config": {"columns": ["report_month", "amount_native"]},
            "reason": "Create analytics layout",
        },
    )
    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "template_id": template_response.json()["id"],
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Reject mismatched template",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))
        audit_events = session.scalars(select(AuditLogORM)).all()

    assert template_response.status_code == 201
    assert response.status_code == 422
    assert "export_type must match" in response.json()["detail"]
    assert export_count == 0
    assert [event.event_type for event in audit_events] == ["EXPORT_TEMPLATE_CHANGED"]


def test_channel_export_request_rejects_unknown_channel_scope(tmp_path):
    """Verify unknown channel export scopes fail before job or audit creation."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "channel",
            "scope_id": "missing-channel",
            "month": "2026-03",
            "currency": "USD",
            "reason": "Reject unknown channel",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found: missing-channel"
    assert export_count == 0
    assert audit_count == 0


def test_export_request_denies_missing_export_permission_before_scope_lookup(tmp_path):
    """Verify missing export permission fails before group scope lookup side effects."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("assistant_analyst"),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": "missing-group-id",
            "month": "2026-03",
            "currency": "USD",
            "reason": "Probe missing group",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
    assert export_count == 0
    assert audit_count == 0


def test_group_export_request_freezes_member_channels_at_creation(tmp_path):
    """Mutating a group after queueing an export must not change the snapshot.

    Codex P2: snapshot group members when creating export jobs so reads and
    downloads return deterministic data per export_id.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="finance-group-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.global_scope(),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(GROUP_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Group analytics snapshot",
        },
    )

    assert response.status_code == 202
    assert sorted(response.json()["scope_channel_ids"]) == ["channel-a", "channel-b"]
    export_id = response.json()["id"]

    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            ChannelGroupMemberORM.__table__.delete().where(
                ChannelGroupMemberORM.channel_id == CHANNEL_B_ROW_ID
            )
        )
        session.commit()

    detail_response = client.get(f"/exports/{export_id}")
    list_response = client.get("/exports?limit=10")

    assert detail_response.status_code == 200
    assert sorted(detail_response.json()["scope_channel_ids"]) == [
        "channel-a",
        "channel-b",
    ]
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["items"]] == [export_id]


def test_group_export_read_uses_snapshot_authorization_after_group_deletion(tmp_path):
    """Codex P1: GET /exports/{id} for a group export must remain accessible
    when the source group is later deleted, because authorization should run
    against the channel snapshot frozen at job creation time.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="finance-group-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.global_scope(),
            ),
        ),
    )
    client = TestClient(app)

    create_response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(GROUP_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Group analytics snapshot before deletion",
        },
    )
    assert create_response.status_code == 202
    export_id = create_response.json()["id"]

    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            ChannelGroupMemberORM.__table__.delete().where(
                ChannelGroupMemberORM.group_id == GROUP_ID
            )
        )
        session.execute(ChannelGroupORM.__table__.delete().where(ChannelGroupORM.id == GROUP_ID))
        session.commit()

    detail_response = client.get(f"/exports/{export_id}")

    assert detail_response.status_code == 200
    assert sorted(detail_response.json()["scope_channel_ids"]) == [
        "channel-a",
        "channel-b",
    ]


def test_export_operator_cannot_request_finance_export_without_finance_visibility(tmp_path):
    """Test that an export operator without finance visibility cannot request a finance export."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Unauthorized finance workbook",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.revenue"


def test_finance_export_request_requires_artifact_read_permissions(tmp_path):
    """Test that requesting a finance export requires artifact read permissions."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="limited-finance-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_REVENUE_REPORT,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Finance workbook without month data permissions",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.view_finalized_payments")
    assert export_count == 0


def test_export_operator_cannot_request_analytics_summary_csv_without_revenue_visibility(tmp_path):
    """Analytics-only export users cannot create CSV jobs containing revenue amounts."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    client = TestClient(app)

    response = client.post(
        "/exports",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_revenue_gate_runs_before_group_lookup(tmp_path):
    """Revenue-less analytics export users get a 403 before group scope lookup."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    client = TestClient(app)

    response = client.post(
        "/exports",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(uuid4()),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_revenue_gate_uses_requested_company_before_lookup(tmp_path):
    """Wrong-scope revenue grants return 403 before company existence lookup."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="wrong-scope-revenue@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(uuid4()),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_revenue_gate_masks_empty_company_scope(tmp_path):
    """Wrong-scope revenue grants return 403 for known empty company scopes."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    empty_company_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            OrgUnitORM(
                id=empty_company_id,
                parent_id=SECTOR_ID,
                type="COMPANY",
                name="Empty Company",
                active=True,
            )
        )
        session.commit()

    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="wrong-scope-empty-company@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(empty_company_id),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_analytics_gate_uses_requested_company_before_lookup(tmp_path):
    """Missing analytics view returns 403 for known and unknown company scopes."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="revenue-only-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.global_scope(),
            ),
        ),
    )
    client = TestClient(app)

    for scope_id in (str(COMPANY_A_ID), str(uuid4())):
        response = client.post(
            "/exports",
            json={
                "export_type": "ANALYTICS_SUMMARY_CSV",
                "scope_type": "company",
                "scope_id": scope_id,
                "month": "2026-03",
                "currency": "USD",
                "reason": "Scoped analytics export",
            },
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Missing permission: analytics.view"

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_export_gate_uses_requested_company_before_lookup(tmp_path):
    """Wrong-scope export grants return 403 before company existence lookup."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="wrong-scope-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.global_scope(),
            ),
        ),
    )
    client = TestClient(app)

    for scope_id in (str(COMPANY_B_ID), str(uuid4())):
        response = client.post(
            "/exports",
            json={
                "export_type": "ANALYTICS_SUMMARY_CSV",
                "scope_type": "company",
                "scope_id": scope_id,
                "month": "2026-03",
                "currency": "USD",
                "reason": "Scoped analytics export",
            },
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Missing permission: exports.analytics"

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_company_request_allows_full_child_channel_grants(tmp_path):
    """Channel-level grants covering the company snapshot can create a scoped CSV."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="channel-covered-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.channel("channel-a"),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.channel("channel-a"),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.channel("channel-a"),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 202
    assert response.json()["scope_channel_ids"] == ["channel-a"]
    assert export_count == 1


def test_analytics_csv_wrong_scope_revenue_cannot_probe_group_lookup(tmp_path):
    """Wrong-scope revenue grants get a 403 for unknown groups, not 404."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="wrong-scope-revenue@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(uuid4()),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_wrong_scope_analytics_cannot_probe_group_lookup(tmp_path):
    """Wrong-scope analytics grants get a 403 for unknown groups, not 404."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="wrong-scope-analytics@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.global_scope(),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(uuid4()),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: analytics.view"
    assert export_count == 0
    assert audit_sink.records == []


def test_analytics_csv_group_only_grants_cannot_probe_unknown_group_lookup(tmp_path):
    """Direct group-scope CSV grants get a 403 for unknown groups, not 404."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    audit_sink = InMemoryAuditSink()
    unknown_group_id = str(uuid4())
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="group-only-csv@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.group(unknown_group_id),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.group(unknown_group_id),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.group(unknown_group_id),
            ),
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/exports",
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": unknown_group_id,
            "month": "2026-03",
            "currency": "USD",
            "reason": "Group lookup probe",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        export_count = session.scalar(select(func.count()).select_from(ExportJobORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
    assert export_count == 0
    assert audit_sink.records == []


def test_non_uuid_gateway_actor_can_create_and_list_exports(tmp_path):
    """Test that a non-UUID gateway actor can create an export and list it successfully."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers(
        "finance_admin",
        "company",
        str(COMPANY_A_ID),
        user_id="gateway-subject-export",
    )

    create_response = client.post(
        "/exports",
        headers=headers,
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )
    list_response = client.get("/exports?limit=10", headers=headers)

    assert create_response.status_code == 202
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["items"]] == [create_response.json()["id"]]


def test_export_list_scan_limit_marks_has_more_and_logs_metric(caplog):
    """Test pagination behavior of export listing, ensuring
    has_more is marked and metrics are logged.
    """
    created_at = datetime(2026, 4, 30, 10, 0, tzinfo=UTC)

    class PagedRepository:
        """Provides paginated retrieval of export jobs via the list_jobs method."""

        def __init__(self) -> None:
            """Track the offsets each list_jobs call requests."""
            self.offsets: list[int] = []

        def list_jobs(
            self,
            *,
            requested_by: str,
            limit: int,
            offset: int,
        ) -> ExportJobPage:
            """Retrieve a page of export jobs with the given pagination parameters."""
            assert requested_by == str(USER_ID)
            self.offsets.append(offset)
            return ExportJobPage(
                items=[
                    ExportJobEntry(
                        id=str(uuid4()),
                        export_type="ANALYTICS_SUMMARY_CSV",
                        scope_type="global",
                        scope_id=None,
                        month="2026-03",
                        currency="USD",
                        requested_by=str(USER_ID),
                        status="QUEUED",
                        file_url=None,
                        month_lock_status="LOCKED",
                        include_confidence_notes=True,
                        include_manual_override_notes=True,
                        created_at=created_at,
                        completed_at=None,
                    )
                ],
                limit=limit,
                offset=offset,
                has_more=True,
            )

    class EmptyGroupRegistry:
        """Registry stub with no groups for export authorization tests."""

        @staticmethod
        def list_groups() -> list[object]:
            """Return no groups."""
            return []

        @staticmethod
        def get_group(group_id: str) -> None:
            """Validate the requested group id without returning a group."""
            assert group_id

    repository = PagedRepository()
    user = UserPrincipal(user_id=str(USER_ID), email="no-export-access@example.com")

    with caplog.at_level(logging.WARNING):
        items, has_more = _list_authorized_export_jobs(
            repository=repository,
            user=user,
            org_index=OrgAccessIndex(),
            group_registry=EmptyGroupRegistry(),
            limit=10,
            offset=0,
            max_scan_pages=2,
        )

    assert items == []
    assert has_more is True
    assert repository.offsets == [0, 1]
    assert "metric=export_job_scan_truncated" in caplog.text
    assert "max_scan_pages=2" in caplog.text


def test_export_list_combines_global_and_month_scoped_finance_permissions(tmp_path):
    """Codex P2: a global finance permission must combine with a month-scoped
    grant of the other finance permission. Previously the intersection of
    month IDs collapsed to empty when one side was granted globally.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    finance_export_id = uuid4()
    other_month_export_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                ExportJobORM(
                    id=finance_export_id,
                    export_type="FINANCE_EXCEL",
                    scope_type="company",
                    scope_id=str(COMPANY_A_ID),
                    scope_channel_ids=["channel-a"],
                    month="2026-03",
                    currency="USD",
                    requested_by=USER_ID,
                    status="QUEUED",
                    month_lock_status="LOCKED",
                    include_confidence_notes=True,
                    include_manual_override_notes=True,
                ),
                ExportJobORM(
                    id=other_month_export_id,
                    export_type="FINANCE_EXCEL",
                    scope_type="company",
                    scope_id=str(COMPANY_A_ID),
                    scope_channel_ids=["channel-a"],
                    month="2026-04",
                    currency="USD",
                    requested_by=USER_ID,
                    status="QUEUED",
                    month_lock_status="LOCKED",
                    include_confidence_notes=True,
                    include_manual_override_notes=True,
                ),
            ]
        )
        session.commit()
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="hybrid-finance-permissions@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_REVENUE_REPORT,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
            PermissionGrant(
                Permission.VIEW_REVENUE,
                AccessScope.company(str(COMPANY_A_ID)),
            ),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS,
                AccessScope.global_scope(),
            ),
            PermissionGrant(
                Permission.VIEW_BANK_RECONCILIATION,
                AccessScope.finance_month("2026-03"),
            ),
        ),
    )
    client = TestClient(app)

    response = client.get("/exports?limit=10")

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()["items"]}
    assert str(finance_export_id) in returned_ids
    assert str(other_month_export_id) not in returned_ids


def test_export_list_uses_snapshot_authorization_for_channel_grants(tmp_path):
    """Verify that export listing respects snapshot authorization for channel grants."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ExportJobORM(
                id=export_id,
                export_type="ANALYTICS_SUMMARY_CSV",
                scope_type="company",
                scope_id=str(COMPANY_A_ID),
                scope_channel_ids=["channel-a"],
                month="2026-03",
                currency="USD",
                requested_by=USER_ID,
                status="QUEUED",
                month_lock_status="LOCKED",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="channel-snapshot-export@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.channel("channel-a"),
            ),
            PermissionGrant(
                Permission.VIEW_ANALYTICS,
                AccessScope.channel("channel-a"),
            ),
        ),
    )
    client = TestClient(app)

    response = client.get("/exports?limit=10")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(export_id)]


def test_company_manager_cannot_request_export_for_another_company(tmp_path):
    """Ensure cross-company analytics CSV requests fail before scope details leak."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("company_manager", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_B_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Cross-company export attempt",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_group_export_requires_access_to_every_member_channel(tmp_path):
    """Ensure analytics CSV group requests fail before group membership details leak."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "group",
            "scope_id": str(GROUP_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Mixed group export attempt",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_export_request_rejects_non_usd_currency_until_exchange_rates_exist(tmp_path):
    """Verify that export requests with non-USD currency are
    rejected until exchange rate support is available.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "EUR",
            "reason": "Unsupported exchange-rate export",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "currency must be USD until exchange-rate support is implemented"
    )


def test_export_list_returns_requesting_users_jobs_only(tmp_path):
    """Ensure that export listing returns only jobs requested by the current user."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )
    other_user_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ExportJobORM(
                id=uuid4(),
                export_type="ANALYTICS_SUMMARY_CSV",
                scope_type="company",
                scope_id=str(COMPANY_A_ID),
                month="2026-03",
                currency="USD",
                requested_by=other_user_id,
                status="QUEUED",
                month_lock_status="LOCKED",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        session.commit()

    response = client.get(
        "/exports?limit=10&offset=0",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    assert create_response.status_code == 202
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [create_response.json()["id"]]
    assert response.json()["pagination"] == {
        "limit": 10,
        "offset": 0,
        "returned": 1,
        "has_more": False,
    }


def test_user_without_export_permission_cannot_list_historical_export_jobs(tmp_path):
    """Verify that users without export permissions cannot list historical export jobs."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    response = client.get(
        "/exports?limit=10&offset=0",
        headers=auth_headers("assistant_analyst", "company", str(COMPANY_A_ID)),
    )

    assert create_response.status_code == 202
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"


def test_export_list_applies_current_scope_and_type_permissions(tmp_path):
    """Ensure that export listing applies the user's current scope and type permissions."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    company_a_analytics = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )
    company_b_analytics = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_B_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_B_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Other company analytics export",
        },
    )
    company_a_finance = client.post(
        "/exports",
        headers=auth_headers("finance_admin"),
        json={
            "export_type": "FINANCE_EXCEL",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Finance export before role change",
        },
    )

    response = client.get(
        "/exports?limit=10&offset=0",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    assert company_a_analytics.status_code == 202
    assert company_b_analytics.status_code == 202
    assert company_a_finance.status_code == 202
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [company_a_analytics.json()["id"]]
    assert response.json()["pagination"] == {
        "limit": 10,
        "offset": 0,
        "returned": 1,
        "has_more": False,
    }


def test_export_operator_can_get_own_export_job(tmp_path):
    """Test that an export operator can retrieve their own export
    job and that audit logs are recorded properly.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics export",
        },
    )

    response = client.get(
        f"/exports/{create_response.json()['id']}",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_events = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 200
    assert response.json()["id"] == create_response.json()["id"]
    assert response.json()["file_url"] is None
    assert response.json()["audit_event"]["event_type"] == "EXPORT_VIEWED"
    assert response.json()["audit_event"]["sensitive"] is True
    assert {event.event_type for event in audit_events} == {
        "EXPORT_CREATED",
        "EXPORT_VIEWED",
    }


def test_finance_admin_downloads_scoped_analytics_summary_csv(tmp_path, monkeypatch):
    """Verify revenue-visible analytics CSV download persists a sanitized artifact."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
        json={
            "export_type": "ANALYTICS_SUMMARY_CSV",
            "scope_type": "company",
            "scope_id": str(COMPANY_A_ID),
            "month": "2026-03",
            "currency": "USD",
            "reason": "Scoped analytics CSV",
        },
    )
    export_id = create_response.json()["id"]
    base_ingested_at = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("12.500000"),
                    ingested_at=base_ingested_at,
                ),
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("7.500000"),
                    ingested_at=base_ingested_at + timedelta(minutes=1),
                ),
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("2.250000"),
                    metric_key="estimatedAdRevenue",
                    ingested_at=base_ingested_at + timedelta(minutes=2),
                ),
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("41.000000"),
                    currency_code="EUR",
                    ingested_at=base_ingested_at + timedelta(minutes=3),
                ),
                _analytics_source_row(
                    channel_id="channel-b",
                    amount=Decimal("99.000000"),
                    ingested_at=base_ingested_at + timedelta(minutes=4),
                ),
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("88.000000"),
                    month="2026-02",
                    ingested_at=base_ingested_at + timedelta(minutes=5),
                ),
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("77.000000"),
                    source_system="youtube_reporting",
                    ingested_at=base_ingested_at + timedelta(minutes=6),
                ),
            ]
        )
        session.commit()

    route = f"/exports/{export_id}/analytics-summary.csv"
    prepared = client.get(
        f"{route}?prepare=true",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        prepared_job = session.get(ExportJobORM, UUID(export_id))
        preparation_audits = session.scalars(select(AuditLogORM)).all()

    assert prepared.status_code == 204
    assert prepared.content == b""
    assert prepared.headers["cache-control"] == "no-store"
    assert prepared_job is not None
    assert prepared_job.status == "COMPLETED"
    assert {event.event_type for event in preparation_audits} == {"EXPORT_CREATED"}

    response = client.get(
        route,
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, UUID(export_id))
        audit_events = session.scalars(select(AuditLogORM)).all()

    assert create_response.status_code == 202
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="ums-analytics-summary-2026-03-company.csv"'
    )
    assert "must-not-leak" not in response.text
    assert "analytics-account-secret" not in response.text
    rows = list(csv.DictReader(StringIO(response.text)))
    assert rows == [
        {
            "report_month": "2026-03",
            "source_system": "youtube_analytics",
            "youtube_channel_id": "channel-a",
            "channel_name": "Channel A",
            "metric_key": "estimatedAdRevenue",
            "value_kind": "estimated",
            "currency_code": "USD",
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "source_row_count": "1",
            "amount_native": "2.25",
            "formula": "SUM(google_revenue_source_rows.amount_native)",
            "confidence": "source_rows",
        },
        {
            "report_month": "2026-03",
            "source_system": "youtube_analytics",
            "youtube_channel_id": "channel-a",
            "channel_name": "Channel A",
            "metric_key": "estimatedRevenue",
            "value_kind": "estimated",
            "currency_code": "USD",
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "source_row_count": "2",
            "amount_native": "20",
            "formula": "SUM(google_revenue_source_rows.amount_native)",
            "confidence": "source_rows",
        },
    ]
    persisted_file = (
        artifact_dir / "exports" / export_id / "ums-analytics-summary-2026-03-company.csv"
    )
    assert persisted_file.read_bytes() == response.content
    assert export_job.status == "COMPLETED"
    assert export_job.file_url is not None
    assert export_job.artifact_filename == "ums-analytics-summary-2026-03-company.csv"
    assert export_job.artifact_content_type == "text/csv"
    assert export_job.artifact_byte_size == len(response.content)
    assert export_job.artifact_checksum_sha256 == hashlib.sha256(response.content).hexdigest()
    assert {event.event_type for event in audit_events} == {
        "EXPORT_CREATED",
        "REVENUE_VIEWED",
        "EXPORT_DOWNLOADED",
    }
    revenue_events = [event for event in audit_events if event.event_type == "REVENUE_VIEWED"]
    assert len(revenue_events) == 1
    revenue_event = revenue_events[0]
    assert revenue_event.scope_type == "channel"
    assert revenue_event.scope_id == "channel-a"
    assert revenue_event.sensitive is True
    assert revenue_event.details["export_type"] == "ANALYTICS_SUMMARY_CSV"
    assert revenue_event.details["artifact_type"] == "analytics_summary_csv"
    downloaded_events = [event for event in audit_events if event.event_type == "EXPORT_DOWNLOADED"]
    assert len(downloaded_events) == 1
    downloaded_event = downloaded_events[0]
    assert downloaded_event.scope_type == "export"
    assert downloaded_event.scope_id == export_id
    assert downloaded_event.sensitive is True
    assert downloaded_event.details["export_type"] == "ANALYTICS_SUMMARY_CSV"
    assert downloaded_event.details["artifact_type"] == "analytics_summary_csv"
    assert downloaded_event.details["artifact_metadata_complete"] is True
    assert downloaded_event.details["artifact_content_type"] == "text/csv"


def test_analytics_summary_csv_download_filters_blank_channel_ids(tmp_path, monkeypatch):
    """Global analytics CSV downloads exclude source rows without a real channel ID."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(
        database_url,
        scope_type="global",
        scope_id=None,
        scope_channel_ids=None,
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                _analytics_source_row(
                    channel_id="channel-a",
                    amount=Decimal("12.500000"),
                ),
                _analytics_source_row(
                    channel_id="",
                    amount=Decimal("99.000000"),
                ),
                _analytics_source_row(
                    channel_id="   ",
                    amount=Decimal("88.000000"),
                ),
                _analytics_source_row(
                    channel_id="\t",
                    amount=Decimal("77.000000"),
                ),
                _analytics_source_row(
                    channel_id="\n",
                    amount=Decimal("66.000000"),
                ),
            ]
        )
        session.commit()

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_admin"),
    )

    assert response.status_code == 200
    rows = list(csv.DictReader(StringIO(response.text)))
    assert [row["youtube_channel_id"] for row in rows] == ["channel-a"]
    assert rows[0]["amount_native"] == "12.5"


def test_analytics_summary_csv_download_requires_analytics_export_permission(tmp_path, monkeypatch):
    """Users with only finance export permission cannot download analytics CSV artifacts."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url)
    engine = create_engine(database_url)

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_approver", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


def test_analytics_summary_csv_download_requires_authenticated_principal(tmp_path, monkeypatch):
    """Unauthenticated callers cannot generate artifacts or write download audit."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url)
    engine = create_engine(database_url)

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv"
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing authentication headers"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


def test_analytics_summary_csv_download_requires_revenue_visibility(tmp_path, monkeypatch):
    """Analytics export-only users cannot download CSVs containing revenue amounts."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url)
    engine = create_engine(database_url)

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


def test_analytics_summary_csv_download_enforces_snapshot_scope(tmp_path, monkeypatch):
    """A caller scoped to company A cannot download a company B export snapshot."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(
        database_url,
        scope_id=str(COMPANY_B_ID),
        scope_channel_ids=("channel-b",),
    )
    engine = create_engine(database_url)

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


def test_analytics_summary_csv_download_rejects_group_only_grants_for_snapshot(
    tmp_path, monkeypatch
):
    """Group grants alone cannot bypass the frozen member-channel CSV snapshot."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(
        database_url,
        scope_type="group",
        scope_id=str(GROUP_ID),
        scope_channel_ids=("channel-a", "channel-b"),
    )
    engine = create_engine(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="group-only-csv@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.EXPORT_ANALYTICS_REPORT,
                AccessScope.group(str(GROUP_ID)),
            ),
            PermissionGrant(Permission.VIEW_ANALYTICS, AccessScope.group(str(GROUP_ID))),
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.group(str(GROUP_ID))),
        ),
    )

    response = TestClient(app).get(f"/exports/{export_id}/analytics-summary.csv")

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


def test_analytics_summary_csv_download_honors_declared_scope_after_org_drift(
    tmp_path, monkeypatch
):
    """Company-scoped queued CSVs remain downloadable after channel membership moves."""
    artifact_dir = tmp_path / "export-artifacts"
    monkeypatch.setenv("UMS_EXPORT_ARTIFACT_DIR", str(artifact_dir))
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        channel = session.get(YouTubeChannelORM, CHANNEL_A_ROW_ID)
        assert channel is not None
        channel.primary_org_unit_id = COMPANY_B_ID
        session.add(
            _analytics_source_row(
                channel_id="channel-a",
                amount=Decimal("12.500000"),
            )
        )
        session.commit()

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    assert response.status_code == 200
    rows = list(csv.DictReader(StringIO(response.text)))
    assert [row["youtube_channel_id"] for row in rows] == ["channel-a"]
    assert rows[0]["amount_native"] == "12.5"


def test_analytics_summary_csv_storage_failure_is_retryable_without_audit(tmp_path):
    """Artifact storage failures leave the export queued and do not emit download audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            _analytics_source_row(
                channel_id="channel-a",
                amount=Decimal("12.500000"),
            )
        )
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_export_artifact_store] = lambda: FileSystemExportArtifactStore(
        tmp_path / "tiny-artifacts",
        max_artifact_size_bytes=1,
    )

    response = TestClient(app).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 503
    assert response.json()["detail"] == "Export artifact storage unavailable"
    assert export_job.status == "QUEUED"
    assert export_job.file_url is None
    assert audit_count == 0


@pytest.mark.parametrize("terminal_status", ["FAILED", "CANCELLED"])
def test_analytics_summary_csv_terminal_job_rejects_before_source_row_read(
    tmp_path, monkeypatch, terminal_status
):
    """Terminal analytics CSV jobs reject before builder source-row reads."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = _seed_analytics_csv_export_job(database_url, job_status=terminal_status)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            _analytics_source_row(
                channel_id="channel-a",
                amount=Decimal("12.500000"),
            )
        )
        session.commit()

    def fail_build(*_args: object, **_kwargs: object) -> None:
        """Fail the test if the source-row export builder is ever called."""
        raise AssertionError("terminal CSV jobs must not build source-row exports")

    monkeypatch.setattr(
        "ums_smart_revenue.api.exports.build_analytics_summary_csv",
        fail_build,
    )

    response = TestClient(create_app(database_url=database_url)).get(
        f"/exports/{export_id}/analytics-summary.csv",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
    )

    with Session(engine) as session:
        export_job = session.get(ExportJobORM, export_id)
        audit_count = session.scalar(select(func.count()).select_from(AuditLogORM))

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Export job is already in terminal status {terminal_status}"
    )
    assert export_job.status == terminal_status
    assert export_job.file_url is None
    assert audit_count == 0


def test_get_export_enforces_scope_even_for_job_owner(tmp_path):
    """Test that get export endpoint enforces scope restrictions even for the job owner."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    export_id = uuid4()
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ExportJobORM(
                id=export_id,
                export_type="ANALYTICS_SUMMARY_CSV",
                scope_type="company",
                scope_id=str(COMPANY_B_ID),
                month="2026-03",
                currency="USD",
                requested_by=USER_ID,
                status="QUEUED",
                month_lock_status="LOCKED",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/exports/{export_id}",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"


def test_user_without_export_permission_cannot_probe_export_ids(tmp_path):
    """Test that a user without export permissions receives 403 when accessing export endpoints."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/exports/not-a-uuid",
        headers=auth_headers("assistant_analyst", "company", str(COMPANY_A_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
