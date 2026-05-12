from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM


ADMIN_ID = UUID("00000000-0000-0000-0000-000000017001")


def auth_headers(role: str, user_id: UUID = ADMIN_ID) -> dict[str, str]:
    return {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'user-accounts.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=ADMIN_ID, email="admin@example.com", display_name="Admin User"))
        session.commit()


def test_corporate_admin_creates_human_user_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "analyst@example.com",
            "display_name": "Analyst User",
            "reason": "Create analyst account for onboarding",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        user = session.scalars(select(UserORM).where(UserORM.email == "analyst@example.com")).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["email"] == "analyst@example.com"
    assert response.json()["display_name"] == "Analyst User"
    assert response.json()["status"] == "active"
    assert response.json()["is_service_account"] is False
    assert response.json()["audit_event"]["event_type"] == "USER_ACCOUNT_CHANGED"
    assert user.status == "active"
    assert user.is_service_account is False
    assert audit_log.event_type == "USER_ACCOUNT_CHANGED"
    assert audit_log.reason == "Create analyst account for onboarding"
    assert audit_log.sensitive is True


def test_assistant_cannot_create_user_accounts(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("assistant_analyst"),
        json={
            "email": "blocked@example.com",
            "display_name": "Blocked User",
            "reason": "Should not be allowed",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: users.manage"


def test_duplicate_user_email_is_rejected_case_insensitively(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    first = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "analyst@example.com",
            "display_name": "Analyst User",
            "reason": "Create first account",
        },
    )
    second = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "Analyst@Example.com",
            "display_name": "Duplicate User",
            "reason": "Attempt duplicate account",
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "User email already exists"


def test_corporate_admin_cannot_create_service_account(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "svc-youtube@example.com",
            "display_name": "YouTube Connector Service",
            "is_service_account": True,
            "reason": "Attempt service account creation",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Service account management requires Super Owner"


def test_super_owner_creates_service_account(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("super_owner"),
        json={
            "email": "svc-youtube@example.com",
            "display_name": "YouTube Connector Service",
            "is_service_account": True,
            "reason": "Create connector service account",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        service_user = session.scalars(select(UserORM).where(UserORM.email == "svc-youtube@example.com")).one()

    assert response.status_code == 201
    assert response.json()["status"] == "service"
    assert response.json()["is_service_account"] is True
    assert service_user.status == "service"
    assert service_user.is_service_account is True


def test_corporate_admin_updates_user_status_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "analyst@example.com",
            "display_name": "Analyst User",
            "reason": "Create analyst account",
        },
    )
    assert create_response.status_code == 201
    user_id = create_response.json()["id"]

    response = client.patch(
        f"/users/{user_id}",
        headers=auth_headers("corporate_admin"),
        json={
            "display_name": "Disabled Analyst",
            "status": "disabled",
            "reason": "Offboarding request",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        user = session.get(UserORM, UUID(user_id))
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.created_at)).all()

    assert response.status_code == 200
    assert response.json()["display_name"] == "Disabled Analyst"
    assert response.json()["status"] == "disabled"
    assert user is not None
    assert user.display_name == "Disabled Analyst"
    assert user.status == "disabled"
    assert [log.event_type for log in audit_logs] == ["USER_ACCOUNT_CHANGED", "USER_ACCOUNT_CHANGED"]
    assert audit_logs[-1].reason == "Offboarding request"


def test_user_update_requires_at_least_one_account_field(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "analyst@example.com",
            "display_name": "Analyst User",
            "reason": "Create analyst account",
        },
    )
    assert create_response.status_code == 201

    response = client.patch(
        f"/users/{create_response.json()['id']}",
        headers=auth_headers("corporate_admin"),
        json={"reason": "No-op updates should not be audited"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 422
    assert len(audit_logs) == 1


def test_assistant_cannot_update_user_or_probe_user_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.patch(
        "/users/not-a-uuid",
        headers=auth_headers("assistant_analyst"),
        json={
            "status": "disabled",
            "reason": "Should be denied before id parsing",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: users.manage"
