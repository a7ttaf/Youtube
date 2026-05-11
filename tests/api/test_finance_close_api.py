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
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM


USER_ID = UUID("00000000-0000-0000-0000-000000005001")


def auth_headers(role: str, scope_type: str = "finance-month", scope_id: str = "2026-03") -> dict[str, str]:
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": "finance-close@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-scope-id": scope_id,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'finance-close.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="finance-close@example.com", display_name="Finance Close User"))
        session.commit()


def test_finance_admin_can_lock_month_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Final payment reconciliation completed"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["month"] == "2026-03"
    assert response.json()["status"] == "LOCKED"
    assert close.locked_by == USER_ID
    assert audit_log.event_type == "MONTH_LOCKED"
    assert audit_log.reason == "Final payment reconciliation completed"


def test_finance_close_rejects_invalid_month_path(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/finance-close/2026-19",
        headers=auth_headers("finance_admin", scope_id="2026-19"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "month must use YYYY-MM with a calendar month from 01 to 12"


def test_get_finance_close_does_not_create_missing_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/finance-close/2026-03", headers=auth_headers("finance_admin"))

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")

    assert response.status_code == 404
    assert response.json()["detail"] == "Finance month close record not found"
    assert close is None


def test_finance_admin_cannot_lock_already_locked_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID))
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should be rejected"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Finance month cannot be locked from its current state"


def test_finance_admin_cannot_lock_month_with_pending_manual_override(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            RevenueManualOverrideORM(
                id=uuid4(),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                adjustment_revenue_usd=Decimal("125.50"),
                reason="Pending source correction",
                created_by=USER_ID,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should wait for approval"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "Finance month has unresolved close blockers",
        "blockers": [
            {
                "blocker_type": "PENDING_MANUAL_OVERRIDES",
                "severity": "HIGH",
                "count": 1,
                "message": "1 pending manual override requires approval before locking 2026-03.",
            }
        ],
    }


def test_finance_close_readiness_reports_reconciliation_variance(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("1000.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("930.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    readiness = client.get("/finance-close/2026-03/readiness", headers=auth_headers("finance_admin"))
    lock_response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should wait for reconciliation"},
    )

    assert readiness.status_code == 200
    assert readiness.json() == {
        "month": "2026-03",
        "ready": False,
        "blockers": [
            {
                "blocker_type": "RECONCILIATION_ISSUES",
                "severity": "HIGH",
                "count": 1,
                "message": "1 channel has unresolved reconciliation issues for 2026-03.",
            }
        ],
    }
    assert lock_response.status_code == 409
    lock_detail = lock_response.json()["detail"]
    assert lock_detail["message"] == "Finance month has unresolved close blockers"
    assert lock_detail["blockers"] == [
        {
            "blocker_type": "RECONCILIATION_ISSUES",
            "severity": "HIGH",
            "count": 1,
            "message": "1 channel has unresolved reconciliation issues for 2026-03.",
        }
    ]


def test_finance_viewer_cannot_lock_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_viewer"),
        json={"reason": "Should be denied"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.lock_month"


def test_finance_approver_can_unlock_month_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID))
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/unlock",
        headers=auth_headers("finance_approver"),
        json={"reason": "Bank correction arrived after lock"},
    )

    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["status"] == "OPEN"
    assert close.unlocked_by == USER_ID
    assert audit_log.event_type == "MONTH_UNLOCKED"


def test_finance_approver_cannot_unlock_open_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/unlock",
        headers=auth_headers("finance_approver"),
        json={"reason": "Should be rejected"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Finance month cannot be unlocked from its current state"


def test_export_operator_cannot_change_allocation_rule(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("export_operator"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee"},
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.change_allocation_rule"


def test_finance_admin_can_record_allocation_rule_metadata_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("finance_admin"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee", "basis": "gross_revenue_usd"},
            "reason": "Allocate confirmed transfer fee by gross revenue",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["allocation_method"] == "gross_revenue_proportional"
    assert close.allocation_rule_payload == {"gap_type": "transfer_fee", "basis": "gross_revenue_usd"}
    assert audit_log.event_type == "ALLOCATION_RULE_CHANGED"
    assert audit_log.details["allocation_method"] == "gross_revenue_proportional"


def test_finance_admin_cannot_change_allocation_rule_on_locked_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID))
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("finance_admin"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee"},
            "reason": "Should be rejected while locked",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Finance month allocation rule cannot be changed from its current state"
