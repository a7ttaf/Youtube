from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import current_audit_sink
from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.report_models import ExportJobORM, ReportBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-000000012001")
COMPANY_A_ID = UUID("00000000-0000-0000-0000-000000012101")
COMPANY_B_ID = UUID("00000000-0000-0000-0000-000000012102")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-000000012201")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-000000012202")
GROUP_ID = UUID("00000000-0000-0000-0000-000000012301")
USER_ID = UUID("00000000-0000-0000-0000-000000012401")


def auth_headers(role: str, scope_type: str = "global", scope_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'exports.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                UserORM(id=USER_ID, email="exports@example.com", display_name="Exports User"),
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(id=COMPANY_A_ID, parent_id=SECTOR_ID, type="COMPANY", name="Company A", active=True),
                OrgUnitORM(id=COMPANY_B_ID, parent_id=SECTOR_ID, type="COMPANY", name="Company B", active=True),
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
                ChannelGroupORM(id=GROUP_ID, name="Mixed Group", group_type="CUSTOM_GROUP", active=True),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_A_ROW_ID),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_B_ROW_ID),
                FinanceMonthCloseORM(month="2026-03", status="LOCKED", allocation_rule_payload={}),
            ]
        )
        session.commit()


def test_finance_admin_requests_finance_export_with_audit_and_lock_snapshot(tmp_path):
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

    assert detail_response.status_code == 200
    assert sorted(detail_response.json()["scope_channel_ids"]) == [
        "channel-a",
        "channel-b",
    ]


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
        session.execute(
            ChannelGroupORM.__table__.delete().where(
                ChannelGroupORM.id == GROUP_ID
            )
        )
        session.commit()

    detail_response = client.get(f"/exports/{export_id}")

    assert detail_response.status_code == 200
    assert sorted(detail_response.json()["scope_channel_ids"]) == [
        "channel-a",
        "channel-b",
    ]


def test_export_operator_cannot_request_finance_export_without_finance_visibility(tmp_path):
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
    assert response.json()["detail"] == (
        "Missing permission: finance.view_finalized_payments"
    )
    assert export_count == 0


def test_export_operator_can_request_analytics_export_for_assigned_company(tmp_path):
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

    assert response.status_code == 202
    assert response.json()["export_type"] == "ANALYTICS_SUMMARY_CSV"
    assert response.json()["scope_id"] == str(COMPANY_A_ID)
    assert response.json()["status"] == "QUEUED"
    assert audit_sink.records[0].event_type == "EXPORT_CREATED"
    assert audit_sink.records[0].permission == "exports.analytics"


def test_company_manager_cannot_request_export_for_another_company(tmp_path):
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
    assert response.json()["detail"] == "Missing permission: exports.analytics"


def test_group_export_requires_access_to_every_member_channel(tmp_path):
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
    assert response.json()["detail"] == "Missing permission: exports.analytics"


def test_export_request_rejects_non_usd_currency_until_exchange_rates_exist(tmp_path):
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
    assert response.json()["detail"] == "currency must be USD until exchange-rate support is implemented"


def test_export_list_returns_requesting_users_jobs_only(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
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
    assert response.json()["pagination"] == {"limit": 10, "offset": 0, "returned": 1, "has_more": False}


def test_user_without_export_permission_cannot_list_historical_export_jobs(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
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

    response = client.get(
        "/exports?limit=10&offset=0",
        headers=auth_headers("assistant_analyst", "company", str(COMPANY_A_ID)),
    )

    assert create_response.status_code == 202
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"


def test_export_list_applies_current_scope_and_type_permissions(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    company_a_analytics = client.post(
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
    company_b_analytics = client.post(
        "/exports",
        headers=auth_headers("export_operator", "company", str(COMPANY_B_ID)),
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
    assert [item["id"] for item in response.json()["items"]] == [
        company_a_analytics.json()["id"]
    ]
    assert response.json()["pagination"] == {
        "limit": 10,
        "offset": 0,
        "returned": 1,
        "has_more": False,
    }


def test_export_operator_can_get_own_export_job(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
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

    response = client.get(
        f"/exports/{create_response.json()['id']}",
        headers=auth_headers("export_operator", "company", str(COMPANY_A_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_events = session.scalars(
            select(AuditLogORM).order_by(AuditLogORM.created_at, AuditLogORM.id)
        ).all()

    assert response.status_code == 200
    assert response.json()["id"] == create_response.json()["id"]
    assert response.json()["file_url"] is None
    assert response.json()["audit_event"]["event_type"] == "EXPORT_VIEWED"
    assert response.json()["audit_event"]["sensitive"] is True
    assert [event.event_type for event in audit_events] == [
        "EXPORT_CREATED",
        "EXPORT_VIEWED",
    ]


def test_get_export_enforces_scope_even_for_job_owner(tmp_path):
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
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/exports/not-a-uuid",
        headers=auth_headers("assistant_analyst", "company", str(COMPANY_A_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: exports.analytics"
