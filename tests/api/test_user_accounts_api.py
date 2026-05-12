from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

import ums_smart_revenue.api.users as users_api
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.users import (
    SqlAlchemyUserAccountRepository,
    UserAccountConflictError,
    UserAccountValidationError,
)
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
        session.add(
            UserORM(id=ADMIN_ID, email="admin@example.com", display_name="Admin User")
        )
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
        user = session.scalars(
            select(UserORM).where(UserORM.email == "analyst@example.com")
        ).one()
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


def test_historical_duplicate_user_emails_return_conflict(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(text("DROP INDEX IF EXISTS uq_users_email_lower"))
        session.add_all(
            [
                UserORM(
                    id=uuid4(), email="legacy@example.com", display_name="Legacy User"
                ),
                UserORM(
                    id=uuid4(),
                    email="Legacy@Example.com",
                    display_name="Legacy User Two",
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "LEGACY@example.com",
            "display_name": "Duplicate Legacy User",
            "reason": "Verify historical duplicate conflict handling",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "User email already exists"


@pytest.mark.parametrize(
    "email",
    [
        "a@@example.com",
        "a@example",
        "a@.example.com",
        "a@example.com.",
        "a@exa..mple.com",
        "a user@example.com",
    ],
)
def test_malformed_user_email_is_rejected(tmp_path, email):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": email,
            "display_name": "Invalid Email User",
            "reason": "Reject malformed email",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "email must be a valid email address"


def test_user_repository_rejects_non_string_email(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)

        with pytest.raises(
            UserAccountValidationError,
            match="email must be a non-empty string",
        ):
            repository.create_user(
                email=123,
                display_name="Invalid Email User",
                is_service_account=False,
            )


def test_user_repository_rejects_non_string_user_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)

        with pytest.raises(
            UserAccountValidationError,
            match="user_id must be a valid UUID",
        ):
            repository.get_user(user_id=123)


def test_user_repository_rejects_non_boolean_service_account_flag(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)

        with pytest.raises(
            UserAccountValidationError,
            match="is_service_account must be a boolean",
        ):
            repository.create_user(
                email="service-flag@example.com",
                display_name="Invalid Service Flag User",
                is_service_account="false",
            )


def test_user_repository_maps_email_unique_constraint_to_conflict(
    tmp_path,
    monkeypatch,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)
        monkeypatch.setattr(repository, "_email_exists", lambda *args, **kwargs: False)

        with pytest.raises(
            UserAccountConflictError,
            match="User email already exists",
        ):
            repository.create_user(
                email="admin@example.com",
                display_name="Duplicate Admin User",
                is_service_account=False,
            )


def test_user_repository_maps_unknown_integrity_error_to_conflict(
    tmp_path,
    monkeypatch,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)
        monkeypatch.setattr(repository, "_email_exists", lambda *args, **kwargs: False)

        def fail_flush(*args, **kwargs):
            raise IntegrityError(
                "insert",
                {},
                Exception("CHECK constraint failed: ck_users_service_account_status"),
            )

        monkeypatch.setattr(session, "flush", fail_flush)

        with pytest.raises(
            UserAccountConflictError,
            match="User account violates database constraints",
        ):
            repository.create_user(
                email="constraint@example.com",
                display_name="Constraint User",
                is_service_account=False,
            )


def test_user_repository_retries_transient_create_storage_error(
    tmp_path,
    monkeypatch,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)
        original_flush = session.flush
        flush_attempts = 0

        def flaky_flush(*args, **kwargs):
            nonlocal flush_attempts
            flush_attempts += 1
            if flush_attempts == 1:
                raise SQLAlchemyTimeoutError("temporary timeout")
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", flaky_flush)

        account = repository.create_user(
            email="retry@example.com",
            display_name="Retry User",
            is_service_account=False,
        )

    assert account.email == "retry@example.com"
    assert flush_attempts > 1


def test_user_repository_returns_conflict_for_concurrent_duplicate_create(
    tmp_path,
    monkeypatch,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session)
        original_flush = session.flush
        injected_duplicate = False

        def flush_with_concurrent_duplicate(*args, **kwargs):
            nonlocal injected_duplicate
            if not injected_duplicate:
                injected_duplicate = True
                with Session(engine) as other_session:
                    other_session.add(
                        UserORM(
                            id=uuid4(),
                            email="race@example.com",
                            display_name="Race Winner",
                        )
                    )
                    other_session.commit()
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", flush_with_concurrent_duplicate)

        with pytest.raises(
            UserAccountConflictError,
            match="User email already exists",
        ):
            repository.create_user(
                email="Race@Example.com",
                display_name="Race Loser",
                is_service_account=False,
            )


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
    assert (
        response.json()["detail"] == "Service account management requires Super Owner"
    )


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
        service_user = session.scalars(
            select(UserORM).where(UserORM.email == "svc-youtube@example.com")
        ).one()

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
        audit_logs = session.scalars(
            select(AuditLogORM).order_by(AuditLogORM.created_at, AuditLogORM.id)
        ).all()

    assert response.status_code == 200
    assert response.json()["display_name"] == "Disabled Analyst"
    assert response.json()["status"] == "disabled"
    assert user is not None
    assert user.display_name == "Disabled Analyst"
    assert user.status == "disabled"
    assert [log.event_type for log in audit_logs] == [
        "USER_ACCOUNT_CHANGED",
        "USER_ACCOUNT_CHANGED",
    ]
    assert audit_logs[-1].reason == "Offboarding request"


def test_create_user_rolls_back_account_when_audit_recording_fails(
    tmp_path,
    monkeypatch,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    def fail_audit_recording(**kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(users_api, "record_audit_event", fail_audit_recording)
    client = TestClient(
        create_app(database_url=database_url),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/users",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "audit-failure@example.com",
            "display_name": "Audit Failure User",
            "reason": "Exercise audit rollback",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        persisted_user = session.scalars(
            select(UserORM).where(UserORM.email == "audit-failure@example.com")
        ).one_or_none()
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 503
    assert response.json()["detail"] == "Audit logging unavailable"
    assert persisted_user is None
    assert audit_logs == []


def test_update_to_historical_duplicate_email_returns_conflict(tmp_path):
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

    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(text("DROP INDEX IF EXISTS uq_users_email_lower"))
        session.add_all(
            [
                UserORM(
                    id=uuid4(), email="legacy@example.com", display_name="Legacy User"
                ),
                UserORM(
                    id=uuid4(),
                    email="Legacy@Example.com",
                    display_name="Legacy User Two",
                ),
            ]
        )
        session.commit()

    response = client.patch(
        f"/users/{create_response.json()['id']}",
        headers=auth_headers("corporate_admin"),
        json={
            "email": "legacy@example.com",
            "reason": "Verify update duplicate conflict handling",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "User email already exists"


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
