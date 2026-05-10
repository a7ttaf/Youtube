from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM, RevenueManualOverrideORM
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM


SECTOR_ID = UUID("00000000-0000-0000-0000-000000009101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000009201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000009301")
FINANCE_ADMIN_ID = UUID("00000000-0000-0000-0000-000000009401")
FINANCE_APPROVER_ID = UUID("00000000-0000-0000-0000-000000009402")


def auth_headers(role: str, user_id: UUID, scope_type: str = "company", scope_id: str | None = None) -> dict[str, str]:
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
    return f"sqlite+pysqlite:///{(tmp_path / 'manual-overrides.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY", name="TV Company", active=True),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(id=FINANCE_ADMIN_ID, email="finance-admin@example.com", display_name="Finance Admin"),
                UserORM(id=FINANCE_APPROVER_ID, email="finance-approver@example.com", display_name="Finance Approver"),
            ]
        )
        if locked_month:
            session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=FINANCE_APPROVER_ID))
        session.commit()


def test_finance_admin_creates_pending_manual_override_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers("finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Correct CMS transfer-fee allocation",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        override = session.scalars(select(RevenueManualOverrideORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["status"] == "PENDING"
    assert response.json()["adjustment_revenue_usd"] == "125.5"
    assert override.created_by == FINANCE_ADMIN_ID
    assert override.adjustment_revenue_usd == Decimal("125.50")
    assert audit_log.event_type == "MANUAL_OVERRIDE_CREATED"
    assert audit_log.reason == "Correct CMS transfer-fee allocation"
    assert audit_log.sensitive is True


def test_finance_approver_approves_pending_manual_override(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    create_response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers("finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Correct CMS transfer-fee allocation",
        },
    )
    approve_response = client.post(
        f"/revenue/manual-overrides/{create_response.json()['id']}/approve",
        headers=auth_headers("finance_approver", FINANCE_APPROVER_ID, scope_id=str(COMPANY_ID)),
        json={"reason": "Approved after source report review"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        override = session.scalars(select(RevenueManualOverrideORM)).one()
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.event_type)).all()

    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "APPROVED"
    assert override.approved_by == FINANCE_APPROVER_ID
    assert {log.event_type for log in audit_logs} == {"MANUAL_OVERRIDE_APPROVED", "MANUAL_OVERRIDE_CREATED"}


def test_manual_override_rejects_locked_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers("finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Should be blocked after close",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Finance month is locked for manual overrides"


def test_company_manager_cannot_create_manual_override(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers("company_manager", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.create_manual_override"
