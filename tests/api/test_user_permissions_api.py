from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.db.security_models import (
    AuditLogORM,
    PermissionORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
)

ADMIN_ID = UUID("00000000-0000-0000-0000-000000015001")
TARGET_ID = UUID("00000000-0000-0000-0000-000000015002")
COMPANY_ID = "company-tv-a"


def auth_headers(role: str, user_id: UUID = ADMIN_ID) -> dict[str, str]:
    return {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'user-permissions.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                UserORM(id=ADMIN_ID, email="admin@example.com", display_name="Admin User"),
                UserORM(id=TARGET_ID, email="target@example.com", display_name="Target User"),
            ]
        )
        for definition in ROLE_DEFINITIONS.values():
            session.add(
                RoleORM(
                    key=definition.role.value,
                    label=definition.label,
                    description=definition.description,
                    service_only=definition.service_only,
                )
            )
        for definition in PERMISSION_DEFINITIONS.values():
            session.add(
                PermissionORM(
                    key=definition.permission.value,
                    label=definition.label,
                    sensitive=definition.sensitive,
                    audit_on_use=definition.audit_on_use,
                )
            )
        session.commit()


def test_finance_admin_grants_scoped_revenue_permission_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("finance_admin"),
        json={
            "permission_key": "finance.view_revenue",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Temporary finance support for month-end checks",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        grant = session.scalars(select(UserPermissionGrantORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["permission_key"] == "finance.view_revenue"
    assert response.json()["scope_type"] == "company"
    assert response.json()["scope_id"] == COMPANY_ID
    assert response.json()["audit_event"]["event_type"] == "USER_PERMISSION_CHANGED"
    assert grant.user_id == TARGET_ID
    assert grant.active is True
    assert audit_log.event_type == "USER_PERMISSION_CHANGED"
    assert audit_log.reason == "Temporary finance support for month-end checks"
    assert audit_log.sensitive is True


def test_corporate_admin_can_grant_non_finance_permission(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("corporate_admin"),
        json={
            "permission_key": "analytics.view_confidence",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Allow QA review of confidence labels",
        },
    )

    assert response.status_code == 201
    assert response.json()["permission_key"] == "analytics.view_confidence"
    assert response.json()["scope_type"] == "company"


def test_corporate_admin_cannot_grant_finance_permission(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("corporate_admin"),
        json={
            "permission_key": "finance.view_revenue",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Attempt finance grant without finance authority",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance permissions require Finance Admin or Super Owner"


def test_finalized_payment_permission_rejects_org_scope_grant(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("finance_admin"),
        json={
            "permission_key": "finance.view_finalized_payments",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Attempt unusable payment visibility grant",
        },
    )

    assert response.status_code == 422
    assert "allowed: finance-month, global" in response.json()["detail"]


def test_finalized_payment_permission_allows_finance_month_grant(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("finance_admin"),
        json={
            "permission_key": "finance.view_finalized_payments",
            "scope_type": "finance-month",
            "scope_id": "2026-03",
            "reason": "Temporary month payment visibility",
        },
    )

    assert response.status_code == 201
    assert response.json()["scope_type"] == "finance-month"
    assert response.json()["scope_id"] == "2026-03"


def test_assistant_cannot_grant_permissions_or_probe_users(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users/not-a-uuid/permissions",
        headers=auth_headers("assistant_analyst"),
        json={
            "permission_key": "analytics.view_confidence",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Should be denied before id parsing",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: roles.assign"


def test_finance_admin_revokes_finance_permission_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("finance_admin"),
        json={
            "permission_key": "finance.view_revenue",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Temporary finance visibility",
        },
    )
    assert create_response.status_code == 201

    response = client.post(
        f"/users/{TARGET_ID}/permissions/{create_response.json()['id']}/revoke",
        headers=auth_headers("finance_admin"),
        json={"reason": "Temporary visibility ended"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        grant = session.scalars(select(UserPermissionGrantORM)).one()
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.created_at)).all()

    assert response.status_code == 200
    assert response.json()["active"] is False
    assert grant.active is False
    assert grant.revoked_by == ADMIN_ID
    assert [log.event_type for log in audit_logs] == [
        "USER_PERMISSION_CHANGED",
        "USER_PERMISSION_CHANGED",
    ]
    assert audit_logs[-1].reason == "Temporary visibility ended"


def test_corporate_admin_cannot_revoke_finance_permission(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    create_response = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("finance_admin"),
        json={
            "permission_key": "finance.view_revenue",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant finance visibility",
        },
    )
    assert create_response.status_code == 201

    response = client.post(
        f"/users/{TARGET_ID}/permissions/{create_response.json()['id']}/revoke",
        headers=auth_headers("corporate_admin"),
        json={"reason": "Attempt revoke without finance authority"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance permissions require Finance Admin or Super Owner"


def test_duplicate_active_permission_grant_is_rejected(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    payload = {
        "permission_key": "analytics.view_confidence",
        "scope_type": "company",
        "scope_id": COMPANY_ID,
        "reason": "First grant",
    }

    first = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("corporate_admin"),
        json=payload,
    )
    second = client.post(
        f"/users/{TARGET_ID}/permissions",
        headers=auth_headers("corporate_admin"),
        json={**payload, "reason": "Duplicate grant"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "Active permission grant already exists"
