from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-000000009101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000009201")
OTHER_COMPANY_ID = UUID("00000000-0000-0000-0000-000000009202")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000009301")
OTHER_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000009302")
FINANCE_ADMIN_ID = UUID("00000000-0000-0000-0000-000000009401")
FINANCE_APPROVER_ID = UUID("00000000-0000-0000-0000-000000009402")


def auth_headers(
    role: str, user_id: UUID, scope_type: str = "company", scope_id: str | None = None
) -> dict[str, str]:
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
                OrgUnitORM(
                    id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True
                ),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                OrgUnitORM(
                    id=OTHER_COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="News Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=OTHER_CHANNEL_ROW_ID,
                    youtube_channel_id="channel-news-a",
                    channel_name="News A",
                    primary_org_unit_id=OTHER_COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(
                    id=FINANCE_ADMIN_ID,
                    email="finance-admin@example.com",
                    display_name="Finance Admin",
                ),
                UserORM(
                    id=FINANCE_APPROVER_ID,
                    email="finance-approver@example.com",
                    display_name="Finance Approver",
                ),
            ]
        )
        if locked_month:
            session.add(
                FinanceMonthCloseORM(
                    month="2026-03", status="LOCKED", locked_by=FINANCE_APPROVER_ID
                )
            )
        session.commit()


def test_finance_admin_creates_pending_manual_override_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
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
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Correct CMS transfer-fee allocation",
        },
    )
    approve_response = client.post(
        f"/revenue/manual-overrides/{create_response.json()['id']}/approve",
        headers=auth_headers(
            "finance_approver", FINANCE_APPROVER_ID, scope_id=str(COMPANY_ID)
        ),
        json={"reason": "Approved after source report review"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        override = session.scalars(select(RevenueManualOverrideORM)).one()
        audit_logs = session.scalars(
            select(AuditLogORM).order_by(AuditLogORM.event_type)
        ).all()

    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "APPROVED"
    assert override.approved_by == FINANCE_APPROVER_ID
    assert {log.event_type for log in audit_logs} == {
        "MANUAL_OVERRIDE_APPROVED",
        "MANUAL_OVERRIDE_CREATED",
    }


def test_manual_override_creator_cannot_approve_own_override(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    create_response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Correct CMS transfer-fee allocation",
        },
    )
    approve_response = client.post(
        f"/revenue/manual-overrides/{create_response.json()['id']}/approve",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={"reason": "Should require a second approver"},
    )

    assert approve_response.status_code == 422
    assert (
        approve_response.json()["detail"]
        == "Manual override creator cannot approve their own override"
    )


def test_manual_override_rejects_locked_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
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
        headers=auth_headers(
            "company_manager", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "Missing permission: finance.create_manual_override"
    )


def test_user_without_approval_permission_cannot_probe_manual_override_ids(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/manual-overrides/not-a-uuid/approve",
        headers=auth_headers(
            "company_manager", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={"reason": "Should be denied before override lookup"},
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "Missing permission: finance.approve_manual_override"
    )


def test_scoped_finance_approver_gets_not_found_for_out_of_scope_override(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        override = RevenueManualOverrideORM(
            id=uuid4(),
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            adjustment_revenue_usd=Decimal("125.50"),
            reason="Correct CMS transfer-fee allocation",
            created_by=FINANCE_ADMIN_ID,
        )
        session.add(override)
        session.commit()
        override_id = str(override.id)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/revenue/manual-overrides/{override_id}/approve",
        headers=auth_headers(
            "finance_approver", FINANCE_APPROVER_ID, scope_id=str(OTHER_COMPANY_ID)
        ),
        json={"reason": "Should not reveal another company's override"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Manual override not found"


def test_finance_viewer_reads_adjusted_revenue_summary_with_approved_overrides_only(
    tmp_path,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    approved_create = client.post(
        "/revenue/manual-overrides",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "125.50",
            "reason": "Correct CMS transfer-fee allocation",
        },
    )
    client.post(
        f"/revenue/manual-overrides/{approved_create.json()['id']}/approve",
        headers=auth_headers(
            "finance_approver", FINANCE_APPROVER_ID, scope_id=str(COMPANY_ID)
        ),
        json={"reason": "Approved after source report review"},
    )
    client.post(
        "/revenue/manual-overrides",
        headers=auth_headers(
            "finance_admin", FINANCE_ADMIN_ID, scope_id=str(COMPANY_ID)
        ),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "adjustment_revenue_usd": "-50.00",
            "reason": "Pending dispute should not affect adjusted revenue",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            MonthlyChannelRevenueFactORM(
                id=UUID("00000000-0000-0000-0000-000000009501"),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd=Decimal("1000.00"),
                views=250000,
                watch_time_minutes=Decimal("7200.50"),
                confidence_score=Decimal("0.9800"),
                imported_by=FINANCE_ADMIN_ID,
            )
        )
        session.commit()

    response = client.get(
        "/revenue/channels/channel-tv-a/months/2026-03/summary",
        headers=auth_headers(
            "finance_viewer", FINANCE_APPROVER_ID, scope_id=str(COMPANY_ID)
        ),
    )

    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 200
    assert response.json()["baseline_gross_revenue_usd"] == "1000"
    assert response.json()["approved_manual_override_total_usd"] == "125.5"
    assert response.json()["adjusted_gross_revenue_usd"] == "1125.5"
    assert response.json()["pending_manual_override_count"] == 1
    assert any(
        log.entity_type == "adjusted_revenue_summary" and log.sensitive
        for log in audit_logs
    )
