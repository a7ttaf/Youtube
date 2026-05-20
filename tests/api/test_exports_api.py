from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
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


def auth_headers(
    role: str, scope_type: str = "global", scope_id: str | None = None
) -> dict[str, str]:
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
                UserORM(
                    id=USER_ID, email="exports@example.com", display_name="Exports User"
                ),
                OrgUnitORM(
                    id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True
                ),
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
                    id=GROUP_ID,
                    name="Mixed Group",
                    group_type="CUSTOM_GROUP",
                    active=True,
                ),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_A_ROW_ID),
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_B_ROW_ID),
                FinanceMonthCloseORM(
                    month="2026-03", status="LOCKED", allocation_rule_payload={}
                ),
            ]
        )
        session.commit()


def test_finance_admin_requests_finance_export_with_audit_and_lock_snapshot(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exports",
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
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
    assert export_job.scope_id == str(COMPANY_A_ID)
    assert export_job.month_lock_status == "LOCKED"
    assert audit_log.event_type == "EXPORT_CREATED"
    assert audit_log.sensitive is True


def test_export_operator_cannot_request_finance_export_without_finance_visibility(
    tmp_path,
):
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


def test_export_operator_can_request_analytics_export_for_assigned_company(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

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
        headers=auth_headers("finance_admin", "company", str(COMPANY_A_ID)),
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
    assert [item["id"] for item in response.json()["items"]] == [
        create_response.json()["id"]
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

    assert response.status_code == 200
    assert response.json()["id"] == create_response.json()["id"]
    assert response.json()["file_url"] is None


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
