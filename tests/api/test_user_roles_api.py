from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.db.security_models import (
    AuditLogORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)

ADMIN_ID = UUID("00000000-0000-0000-0000-000000014001")
TARGET_ID = UUID("00000000-0000-0000-0000-000000014002")
COMPANY_ID = "company-tv-a"


def auth_headers(role: str, user_id: UUID = ADMIN_ID) -> dict[str, str]:
    """Return trusted-gateway headers for one actor and tenant."""
    return {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    """Return the disposable SQLite URL backing these API tests."""
    return f"sqlite+pysqlite:///{(tmp_path / 'user-roles.db').as_posix()}"


def seed_database(database_url: str, *, target_is_service_account: bool = False) -> None:
    """Seed one disposable database for the scenario under test."""
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                UserORM(id=ADMIN_ID, email="admin@example.com", display_name="Admin User"),
                UserORM(
                    id=TARGET_ID,
                    email="target@example.com",
                    display_name="Target User",
                    is_service_account=target_is_service_account,
                ),
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
        session.commit()


def test_corporate_admin_assigns_scoped_assistant_role_with_audit(tmp_path):
    """Corporate admin assigns scoped assistant role with audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant analytics support access",
        },
    )
    assert response.status_code == 201

    engine = create_engine(database_url)
    with Session(engine) as session:
        assignment = session.scalars(select(UserRoleAssignmentORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()
    assert response.json()["role_key"] == "assistant_analyst"
    assert response.json()["scope_type"] == "company"
    assert response.json()["scope_id"] == COMPANY_ID
    assert response.json()["audit_event"]["event_type"] == "USER_ROLE_CHANGED"
    assert assignment.user_id == TARGET_ID
    assert assignment.active is True
    assert audit_log.event_type == "USER_ROLE_CHANGED"
    assert audit_log.reason == "Grant analytics support access"
    assert audit_log.sensitive is True


def test_assign_role_rejects_unknown_actor_before_assignment_write(tmp_path):
    """Assign role rejects unknown actor before assignment write."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    unknown_actor_id = UUID("00000000-0000-0000-0000-000000014999")
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin", user_id=unknown_actor_id),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Reject unknown actor",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        assignments = session.scalars(select(UserRoleAssignmentORM)).all()
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 404
    assert response.json()["detail"] == "Actor user not found"
    assert assignments == []
    assert audit_logs == []


def test_assistant_cannot_assign_roles_or_probe_users(tmp_path):
    """Assistant cannot assign roles or probe users."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/users/not-a-uuid/roles",
        headers=auth_headers("assistant_analyst"),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Should be denied before id parsing",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: roles.assign"


def test_corporate_admin_assigns_company_scoped_export_operator(tmp_path):
    """Corporate admin assigns company scoped export operator."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "export_operator",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant scoped export operations",
        },
    )

    assert response.status_code == 201
    assert response.json()["role_key"] == "export_operator"
    assert response.json()["scope_type"] == "company"
    assert response.json()["scope_id"] == COMPANY_ID


def test_corporate_admin_cannot_assign_finance_role(tmp_path):
    """Corporate admin cannot assign finance role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "finance_viewer",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Attempt finance grant without finance authority",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance roles require Finance Admin or Super Owner"


def test_corporate_admin_cannot_assign_beta_operator(tmp_path):
    """The beta role carries finance-control powers and stays in the finance family."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "beta_operator",
            "scope_type": "global",
            "scope_id": None,
            "reason": "Attempt beta grant without finance authority",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance roles require Finance Admin or Super Owner"


@pytest.mark.parametrize("authorized_role", ["finance_admin", "super_owner"])
def test_finance_authority_can_assign_and_revoke_beta_operator(tmp_path, authorized_role):
    """Finance Admin and Super Owner retain lifecycle authority for the beta role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    created = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers(authorized_role),
        json={
            "role_key": "beta_operator",
            "scope_type": "global",
            "scope_id": None,
            "reason": "Grant beta finance workflow",
        },
    )
    assert created.status_code == 201, created.text

    revoked = client.post(
        f"/users/{TARGET_ID}/roles/{created.json()['id']}/revoke",
        headers=auth_headers(authorized_role),
        json={"reason": "End beta finance workflow"},
    )

    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active"] is False


def test_finance_admin_can_assign_finance_viewer_role(tmp_path):
    """Finance admin can assign finance viewer role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("finance_admin"),
        json={
            "role_key": "finance_viewer",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant read-only finance visibility",
        },
    )

    assert response.status_code == 201
    assert response.json()["role_key"] == "finance_viewer"
    assert response.json()["scope_type"] == "company"


def test_corporate_admin_cannot_assign_super_owner(tmp_path):
    """Corporate admin cannot assign super owner."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "super_owner",
            "scope_type": "global",
            "reason": "Attempt break-glass assignment",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Super Owner assignments require Super Owner"


def test_incompatible_scope_type_rejected_before_persisting(tmp_path):
    """Incompatible scope type rejected before persisting."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    # tv_sector_manager allows only global/sector scopes; company must be
    # rejected at the repository layer (no special policy gate for this
    # role, so scope check is reachable)
    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "tv_sector_manager",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Attempt sector role on company scope",
        },
    )

    assert response.status_code == 422
    assert "cannot be assigned to scope type" in response.json()["detail"]

    # Regression: mixed-case scope_type must be normalised and still rejected
    response_mixed = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "tv_sector_manager",
            "scope_type": "CoMpAnY",
            "scope_id": COMPANY_ID,
            "reason": "Attempt sector role on mixed-case company scope",
        },
    )
    assert response_mixed.status_code == 422
    assert "cannot be assigned to scope type" in response_mixed.json()["detail"]

    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.scalars(select(UserRoleAssignmentORM)).all() == []
        assert session.scalars(select(AuditLogORM)).all() == []


def test_corporate_admin_revokes_role_assignment_with_audit(tmp_path):
    """Corporate admin revokes role assignment with audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("corporate_admin"),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Temporary analytics access",
        },
    )

    assert create_response.status_code == 201
    assignment_id = create_response.json()["id"]
    response = client.post(
        f"/users/{TARGET_ID}/roles/{assignment_id}/revoke",
        headers=auth_headers("corporate_admin"),
        json={"reason": "Temporary access ended"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        assignment = session.scalars(select(UserRoleAssignmentORM)).one()
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.created_at)).all()

    assert response.status_code == 200
    assert response.json()["active"] is False
    assert assignment.active is False
    assert assignment.revoked_by == ADMIN_ID
    assert [log.event_type for log in audit_logs] == [
        "USER_ROLE_CHANGED",
        "USER_ROLE_CHANGED",
    ]
    assert audit_logs[-1].reason == "Temporary access ended"


def test_corporate_admin_cannot_revoke_finance_role(tmp_path):
    """Corporate admin cannot revoke finance role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    create_response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("finance_admin"),
        json={
            "role_key": "finance_viewer",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant finance visibility",
        },
    )
    assert create_response.status_code == 201

    response = client.post(
        f"/users/{TARGET_ID}/roles/{create_response.json()['id']}/revoke",
        headers=auth_headers("corporate_admin"),
        json={"reason": "Attempt revocation without finance authority"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance roles require Finance Admin or Super Owner"


def test_corporate_admin_cannot_revoke_beta_operator(tmp_path):
    """Revocation is finance-sensitive for the same beta role family as assignment."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    created = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("finance_admin"),
        json={
            "role_key": "beta_operator",
            "scope_type": "global",
            "scope_id": None,
            "reason": "Grant beta finance workflow",
        },
    )
    assert created.status_code == 201, created.text

    response = client.post(
        f"/users/{TARGET_ID}/roles/{created.json()['id']}/revoke",
        headers=auth_headers("corporate_admin"),
        json={"reason": "Attempt revocation without finance authority"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Finance roles require Finance Admin or Super Owner"


def test_finance_admin_can_revoke_finance_viewer_role(tmp_path):
    """Finance admin can revoke finance viewer role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    create_response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers("finance_admin"),
        json={
            "role_key": "finance_viewer",
            "scope_type": "company",
            "scope_id": COMPANY_ID,
            "reason": "Grant finance visibility",
        },
    )
    assert create_response.status_code == 201

    response = client.post(
        f"/users/{TARGET_ID}/roles/{create_response.json()['id']}/revoke",
        headers=auth_headers("finance_admin"),
        json={"reason": "Revoke finance visibility"},
    )

    assert response.status_code == 200
    assert response.json()["active"] is False
