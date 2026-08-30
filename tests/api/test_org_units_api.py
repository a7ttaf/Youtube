"""Endpoint tests for the read-only GET /org-units listing.

Gateway-header driven (like test_channels_api.py) over a real SQLite-backed
app built with create_app(database_url=...). Covers: 200 list shape + ordering,
active-only filtering, tenant isolation, the VIEW_ANALYTICS fail-closed gate,
and the scoped analytics viewer still receiving the full active list.
"""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM
from ums_smart_revenue.db.security_models import SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-0000000000a1")
CURRENT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000000002")

SECTOR_TV_ID = UUID("00000000-0000-0000-0000-0000000005e1")
COMPANY_TV_ID = UUID("00000000-0000-0000-0000-0000000000c1")
INACTIVE_COMPANY_ID = UUID("00000000-0000-0000-0000-0000000000c9")
OTHER_TENANT_SECTOR_ID = UUID("00000000-0000-0000-0000-0000000005e2")


def auth_headers(role, scope_type="global", scope_id=None):
    """Return trusted-gateway auth headers for the given role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "org-reader@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    """Return a unique SQLite URL under tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url):
    """Create schema and seed org units across two tenants plus the test user.

    Current tenant: an active SECTOR (TV) parenting an active COMPANY (TV
    Productions), plus an INACTIVE company that must be filtered out. A second
    tenant gets its own active SECTOR that must never appear for tenant #1.
    """
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="org-reader@example.com",
                display_name="Org Reader",
            )
        )
        session.add_all(
            [
                OrgUnitORM(
                    id=COMPANY_TV_ID,
                    parent_id=SECTOR_TV_ID,
                    type="COMPANY",
                    name="TV Productions",
                    active=True,
                    tenant_id=CURRENT_TENANT_ID,
                ),
                OrgUnitORM(
                    id=SECTOR_TV_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV",
                    active=True,
                    tenant_id=CURRENT_TENANT_ID,
                ),
                OrgUnitORM(
                    id=INACTIVE_COMPANY_ID,
                    parent_id=SECTOR_TV_ID,
                    type="COMPANY",
                    name="Retired Company",
                    active=False,
                    tenant_id=CURRENT_TENANT_ID,
                ),
                OrgUnitORM(
                    id=OTHER_TENANT_SECTOR_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="Other Tenant Sector",
                    active=True,
                    tenant_id=OTHER_TENANT_ID,
                ),
            ]
        )
        session.commit()


def test_super_owner_lists_active_units_with_exact_shape_and_ordering(tmp_path):
    """200 returns active current-tenant units, exact item shape, (type, name) order."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/org-units", headers=auth_headers("super_owner", "global"))

    assert response.status_code == 200
    body = response.json()
    # Ordered by (type, name): COMPANY 'TV Productions' precedes SECTOR 'TV'.
    assert body == [
        {
            "id": str(COMPANY_TV_ID),
            "parent_id": str(SECTOR_TV_ID),
            "type": "COMPANY",
            "name": "TV Productions",
            "active": True,
        },
        {
            "id": str(SECTOR_TV_ID),
            "parent_id": None,
            "type": "SECTOR",
            "name": "TV",
            "active": True,
        },
    ]
    # UUIDs are serialized as strings, never raw UUID objects.
    assert all(isinstance(item["id"], str) for item in body)


def test_inactive_unit_excluded(tmp_path):
    """Deactivated units never appear in the listing (active-only)."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/org-units", headers=auth_headers("super_owner", "global"))

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()}
    assert str(INACTIVE_COMPANY_ID) not in returned_ids


def test_other_tenant_unit_excluded(tmp_path):
    """Units belonging to a different tenant are invisible (tenant isolation)."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/org-units", headers=auth_headers("super_owner", "global"))

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()}
    assert str(OTHER_TENANT_SECTOR_ID) not in returned_ids


def test_role_without_view_analytics_is_forbidden(tmp_path):
    """A role lacking VIEW_ANALYTICS gets a fail-closed 403, not an empty list."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))

    # audit_viewer holds only VIEW_AUDIT_LOG -> no VIEW_ANALYTICS grant.
    response = client.get("/org-units", headers=auth_headers("audit_viewer", "global"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: analytics.view"


def test_missing_gateway_token_is_unauthorized(tmp_path):
    """The route rejects a request with no trusted gateway token."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("super_owner")
    headers.pop("x-ums-trusted-gateway-token")

    response = client.get("/org-units", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted gateway token"


def test_invalid_gateway_token_is_unauthorized(tmp_path):
    """The route rejects a caller whose gateway token does not match."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("super_owner")
    headers["x-ums-trusted-gateway-token"] = "wrong-token"

    response = client.get("/org-units", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted gateway token"


def test_unknown_gateway_role_is_rejected_before_route_access(tmp_path):
    """An unknown role cannot be smuggled through the proxy header contract."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("not-a-real-role")

    response = client.get("/org-units", headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown role: not-a-real-role"


def test_disabled_principal_is_forbidden_despite_grant(tmp_path):
    """A disabled principal is rejected fail-closed even WITH a VIEW_ANALYTICS grant.

    The trusted-gateway header path never marks a principal disabled, so the
    gate's disabled branch is exercised by overriding the principal dependency
    directly — proving the route's own guard fails closed rather than relying
    on an upstream dependency to pre-filter disabled users.
    """
    database_url = build_database_url(tmp_path)
    seed(database_url)
    app = create_app(database_url=database_url)

    def _disabled_principal():
        """Disabled principal that still carries a global VIEW_ANALYTICS grant."""
        return UserPrincipal(
            user_id=str(USER_ID),
            email="org-reader@example.com",
            direct_permissions=(
                PermissionGrant(Permission.VIEW_ANALYTICS, AccessScope.global_scope()),
            ),
            disabled=True,
        )

    app.dependency_overrides[current_principal_from_headers] = _disabled_principal
    client = TestClient(app)

    response = client.get("/org-units")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: analytics.view"


def test_scoped_analytics_viewer_gets_full_active_list(tmp_path):
    """A company-scoped analytics viewer still receives the full active list."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/org-units",
        headers=auth_headers("company_manager", "company", str(COMPANY_TV_ID)),
    )

    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()}
    assert returned_ids == {str(COMPANY_TV_ID), str(SECTOR_TV_ID)}
