"""API tests for GET /revenue/scopes (authorized rollup scope options).

Security focus: the endpoint must return ONLY the viewer's VIEW_REVENUE-authorized
rollup scopes. A scoped viewer must never see a foreign company/sector (the whole
reason this endpoint exists instead of reusing the VIEW_ANALYTICS GET /org-units).
A viewer with no VIEW_REVENUE grant gets a fail-closed 403. Names come from the
org-unit reader with a raw-id fallback; no audit event is emitted (metadata read).
"""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_TV_ID = UUID("00000000-0000-0000-0000-0000000051a1")
SECTOR_MUSIC_ID = UUID("00000000-0000-0000-0000-0000000051a2")
COMPANY_TV_A_ID = UUID("00000000-0000-0000-0000-0000000051b1")
COMPANY_TV_B_ID = UUID("00000000-0000-0000-0000-0000000051b2")
COMPANY_MUSIC_ID = UUID("00000000-0000-0000-0000-0000000051b3")
CHANNEL_TV_A_ROW_ID = UUID("00000000-0000-0000-0000-0000000051c1")
CHANNEL_MUSIC_ROW_ID = UUID("00000000-0000-0000-0000-0000000051c2")
USER_ID = UUID("00000000-0000-0000-0000-0000000051d1")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Build trusted-gateway auth headers for the given role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "scopes@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def _principal(*grants: PermissionGrant) -> UserPrincipal:
    """Return a principal carrying exactly the given direct VIEW_REVENUE grants."""
    return UserPrincipal(
        user_id=str(USER_ID),
        email="scopes@example.com",
        direct_permissions=grants,
    )


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed two sectors, three companies, and two channels for scope tests.

    company_sector requires a COMPANY parented to a SECTOR; channels with a
    primary company keep those companies in the access index even if a name map
    lookup is exercised separately.
    """
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_TV_ID, parent_id=None, type="SECTOR",
                    name="TV", active=True,
                ),
                OrgUnitORM(
                    id=SECTOR_MUSIC_ID, parent_id=None, type="SECTOR",
                    name="Music", active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_TV_A_ID, parent_id=SECTOR_TV_ID, type="COMPANY",
                    name="TV A", active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_TV_B_ID, parent_id=SECTOR_TV_ID, type="COMPANY",
                    name="TV B", active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_MUSIC_ID, parent_id=SECTOR_MUSIC_ID, type="COMPANY",
                    name="Music Co", active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_TV_A_ROW_ID, youtube_channel_id="channel-tv-a",
                    channel_name="TV A Channel", primary_org_unit_id=COMPANY_TV_A_ID,
                    cms_status="INSIDE_CMS", revenue_required=True, active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_MUSIC_ROW_ID, youtube_channel_id="channel-music",
                    channel_name="Music Channel", primary_org_unit_id=COMPANY_MUSIC_ID,
                    cms_status="INSIDE_CMS", revenue_required=True, active=True,
                ),
                UserORM(
                    id=USER_ID, email="scopes@example.com",
                    display_name="Scopes User",
                ),
            ]
        )
        session.commit()


def test_global_viewer_lists_global_all_sectors_all_companies(tmp_path):
    """A global VIEW_REVENUE viewer gets global + all sectors + all companies."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal(
        PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")

    assert response.status_code == 200
    scopes = response.json()["scopes"]
    assert scopes[0] == {"scope_type": "global", "scope_id": None, "label": "Global"}
    keys = {(s["scope_type"], s["scope_id"]) for s in scopes}
    assert keys == {
        ("global", None),
        ("sector", str(SECTOR_TV_ID)),
        ("sector", str(SECTOR_MUSIC_ID)),
        ("company", str(COMPANY_TV_A_ID)),
        ("company", str(COMPANY_TV_B_ID)),
        ("company", str(COMPANY_MUSIC_ID)),
    }
    # Names resolved (not raw ids).
    labels = {s["scope_id"]: s["label"] for s in scopes if s["scope_id"]}
    assert labels[str(SECTOR_TV_ID)] == "TV"
    assert labels[str(COMPANY_MUSIC_ID)] == "Music Co"


def test_company_scoped_viewer_lists_only_their_company_no_leak(tmp_path):
    """A company-scoped viewer gets ONLY their company; a foreign company id is
    absent (no org-structure leak — mirrors the net-revenue out-of-scope guard).
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal(
        PermissionGrant(
            Permission.VIEW_REVENUE, AccessScope.company(str(COMPANY_TV_A_ID))
        ),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")

    assert response.status_code == 200
    scopes = response.json()["scopes"]
    assert [(s["scope_type"], s["scope_id"]) for s in scopes] == [
        ("company", str(COMPANY_TV_A_ID))
    ]
    ids = {s["scope_id"] for s in scopes}
    # Foreign company + sibling company + the viewer's own sector must NOT leak.
    assert str(COMPANY_MUSIC_ID) not in ids
    assert str(COMPANY_TV_B_ID) not in ids
    assert str(SECTOR_TV_ID) not in ids
    assert all(s["scope_type"] != "global" for s in scopes)


def test_sector_scoped_viewer_lists_sector_and_its_companies_only(tmp_path):
    """A sector-scoped viewer gets their sector + its companies, not other
    sectors' companies and no global option.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal(
        PermissionGrant(
            Permission.VIEW_REVENUE, AccessScope.sector(str(SECTOR_TV_ID))
        ),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")

    assert response.status_code == 200
    scopes = response.json()["scopes"]
    keys = {(s["scope_type"], s["scope_id"]) for s in scopes}
    assert keys == {
        ("sector", str(SECTOR_TV_ID)),
        ("company", str(COMPANY_TV_A_ID)),
        ("company", str(COMPANY_TV_B_ID)),
    }
    # The Music sector and its company must not appear.
    assert ("sector", str(SECTOR_MUSIC_ID)) not in keys
    assert ("company", str(COMPANY_MUSIC_ID)) not in keys
    assert all(s["scope_type"] != "global" for s in scopes)


def test_viewer_without_view_revenue_is_forbidden(tmp_path):
    """A viewer with no VIEW_REVENUE grant (assistant_analyst) is 403 fail-closed."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/scopes",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_disabled_principal_is_forbidden(tmp_path):
    """A disabled principal is 403 even though it carries a VIEW_REVENUE grant."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="scopes@example.com",
        disabled=True,
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
        ),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_deactivated_company_excluded_from_scope_options(tmp_path):
    """A deactivated company is EXCLUDED from the scope options (fail-closed),
    not raw-id-fallback-listed. Deactivating the unit drops it from BOTH the
    org-unit name map AND the access index (build_org_access_index walks active
    units only), so the sector grant cannot expand to it: it has no id and no
    name in the response. API-layer raw-id fallback for a company is therefore
    unreachable (company_names and company_sector share the active-units source);
    the pure-service fallback path is covered by
    tests/finance/test_revenue_scopes.py::test_raw_id_fallback_when_name_missing.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        company_b = session.get(OrgUnitORM, COMPANY_TV_B_ID)
        company_b.active = False
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal(
        PermissionGrant(
            Permission.VIEW_REVENUE, AccessScope.sector(str(SECTOR_TV_ID))
        ),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")

    assert response.status_code == 200
    scopes = response.json()["scopes"]
    by_id = {s["scope_id"]: s["label"] for s in scopes}
    # The still-active sibling company stays named; the sector keeps its name.
    assert by_id[str(COMPANY_TV_A_ID)] == "TV A"
    assert by_id[str(SECTOR_TV_ID)] == "TV"
    # Fail-closed exclusion / no-leak: the deactivated company's id is absent and
    # its name never appears as any option's label (not even via a raw-id label).
    ids = {s["scope_id"] for s in scopes}
    labels = {s["label"] for s in scopes}
    assert str(COMPANY_TV_B_ID) not in ids
    assert "TV B" not in labels


def test_scopes_endpoint_emits_no_audit_event(tmp_path):
    """The metadata read writes no audit row (consistent with GET /org-units)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal(
        PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
    )
    client = TestClient(app)

    response = client.get("/revenue/scopes")
    assert response.status_code == 200

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_rows = session.scalars(select(AuditLogORM)).all()
    assert audit_rows == []
