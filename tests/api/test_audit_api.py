from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM


USER_ID = UUID("00000000-0000-0000-0000-000000013001")


def auth_headers(role: str) -> dict[str, str]:
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'audit.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    created_at = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="audit@example.com", display_name="Audit User"))
        session.add_all(
            [
                AuditLogORM(
                    id=uuid4(),
                    user_id=USER_ID,
                    event_type="REVENUE_VIEWED",
                    entity_type="monthly_channel_revenue_fact",
                    entity_id="channel-a:2026-03",
                    scope_type="channel",
                    scope_id="channel-a",
                    reason=None,
                    details={"gross_revenue_usd": "1000", "source": "YOUTUBE_CMS"},
                    sensitive=True,
                    created_at=created_at,
                ),
                AuditLogORM(
                    id=uuid4(),
                    user_id=USER_ID,
                    event_type="LOGIN",
                    entity_type="user_session",
                    entity_id=str(USER_ID),
                    scope_type="global",
                    scope_id=None,
                    reason=None,
                    details={"ip": "127.0.0.1"},
                    sensitive=False,
                    created_at=created_at - timedelta(minutes=5),
                ),
            ]
        )
        session.commit()


def test_audit_viewer_lists_audit_events_with_sensitive_details_masked(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/audit/events?limit=10&offset=0", headers=auth_headers("audit_viewer"))

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_events = session.scalars(select(AuditLogORM).order_by(AuditLogORM.created_at)).all()

    assert response.status_code == 200
    assert response.json()["items"][0]["event_type"] == "REVENUE_VIEWED"
    assert response.json()["items"][0]["details"] == {}
    assert response.json()["items"][0]["details_redacted"] is True
    assert response.json()["items"][1]["details"] == {"ip": "127.0.0.1"}
    assert response.json()["items"][1]["details_redacted"] is False
    assert response.json()["audit_event"]["event_type"] == "AUDIT_LOG_VIEWED"
    assert audit_events[-1].event_type == "AUDIT_LOG_VIEWED"

    next_response = client.get("/audit/events?limit=10&offset=0", headers=auth_headers("audit_viewer"))

    assert [item["event_type"] for item in next_response.json()["items"]] == ["REVENUE_VIEWED", "LOGIN"]


def test_super_owner_can_view_sensitive_audit_details(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/audit/events?event_type=REVENUE_VIEWED", headers=auth_headers("super_owner"))

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["details"] == {"gross_revenue_usd": "1000", "source": "YOUTUBE_CMS"}
    assert response.json()["items"][0]["details_redacted"] is False


def test_assistant_cannot_view_audit_events(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/audit/events", headers=auth_headers("assistant_analyst"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: audit.view"
