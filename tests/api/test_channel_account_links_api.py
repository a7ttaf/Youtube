"""Endpoint tests for the channel-account map (auth, audit, payload safety)."""
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import AdsenseContentOwnerLinkORM, FinanceBase
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-0000000d0401")


def auth_headers(role, scope_type="global", scope_id=None):
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "map-admin@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url):
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="map-admin@example.com", display_name="Map Admin"))
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                adsense_account_id="pub-1", content_owner_id="owner-1",
                verification_status="UNVERIFIED", provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={"secret_provenance": "LEAK-1"},
                effective_month_start="2026-01",
            )
        )
        session.commit()


def test_finance_viewer_lists_links_without_provenance_payload(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["links"][0]["adsense_account_id"] == "pub-1"
    assert "provenance_payload" not in str(body)
    assert "LEAK" not in str(body)
    assert {e["event_type"] for e in body["audit_events"]} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = session.scalars(select(AuditLogORM)).all()
    assert {log.event_type for log in logs} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_list_denied_without_finance_view_permissions(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    # corporate_admin holds MANAGE_ORG_MAPPING but no finance-view perms -> 403, fail-closed.
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
    )
    assert response.status_code == 403


def test_list_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links?month=2026-13",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_propose_creates_link_with_org_mapping_permission(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "adsense_account_id": "pub-2", "content_owner_id": "owner-2",
            "effective_month_start": "2026-02", "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {"note": "from contract"},
            "reason": "operator asserts pub-2 maps to owner-2",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["link"]["verification_status"] == "UNVERIFIED"
    assert "provenance_payload" not in str(body)
    assert body["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_PROPOSED"


def test_propose_requires_manage_org_mapping(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("finance_viewer", "global"),  # has finance view, NOT MANAGE_ORG_MAPPING
        json={
            "adsense_account_id": "pub-2", "content_owner_id": "owner-2",
            "effective_month_start": "2026-02", "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED", "provenance_payload": {},
            "reason": "x",
        },
    )
    assert response.status_code == 403
