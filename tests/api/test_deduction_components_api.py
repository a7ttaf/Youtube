"""Tests for the read-only deduction-components endpoint + net-revenue consumption."""

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-0000000e0101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000e0201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000e0301")
USER_ID = UUID("00000000-0000-0000-0000-0000000e0401")
MONTH = "2026-04"
CHANNEL = "channel-tv-a"


def auth_headers(role, scope_type="global", scope_id=None):
    """Build trusted-gateway auth headers."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "deduction-view@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url, *, net_revenue_usd="900.00"):
    """Seed org/security/finance rows; net_revenue_usd=None forces missing-net."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(
                    id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY", name="TV Co", active=True
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id=CHANNEL,
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month=MONTH,
                    youtube_channel_id=CHANNEL,
                    source_kind="ADSENSE",
                    source_report_id="adsense-2026-04",
                    gross_revenue_usd=Decimal("1000.00"),
                    net_revenue_usd=(None if net_revenue_usd is None else Decimal(net_revenue_usd)),
                    views=1000,
                    watch_time_minutes=Decimal("100.00"),
                    confidence_score=Decimal("0.95"),
                    imported_by=USER_ID,
                ),
                UserORM(
                    id=USER_ID, email="deduction-view@example.com", display_name="Deduction Viewer"
                ),
            ]
        )
        session.commit()


def _component(
    *,
    kind,
    scope_kind,
    scope_id,
    amount,
    source_system,
    key_suffix,
    source_table="google_revenue_source_rows",
):
    """Build one DeductionComponentORM row."""
    return DeductionComponentORM(
        id=uuid4(),
        month=MONTH,
        component_kind=kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table=source_table,
        source_id=None,
        source_key=f"k-{key_suffix}",
        source_report_id=None,
        raw_payload={"secret_provenance": f"LEAK-{key_suffix}"},
        component_key=f"key:{key_suffix}",
    )


def _seed_components(database_url):
    """Seed one component of each scope so grouping/audit branches are exercised."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                _component(
                    kind="DEDUCTION",
                    scope_kind="CHANNEL",
                    scope_id=CHANNEL,
                    amount="120.00",
                    source_system="adsense_management",
                    key_suffix="chan",
                ),
                _component(
                    kind="UNRESOLVED_PAYMENT_GAP",
                    scope_kind="ACCOUNT",
                    scope_id="pub-1",
                    amount="70.00",
                    source_system="adsense_payment_gap",
                    key_suffix="acct",
                    source_table="adsense_payment_gap",
                ),
                _component(
                    kind="TRANSFER_FEE",
                    scope_kind="PAYMENT",
                    scope_id="BANK-1",
                    amount="5.00",
                    source_system="bank_reconciliation",
                    key_suffix="pay",
                    source_table="bank_reconciliation_entries",
                ),
            ]
        )
        session.commit()


def _seed_channel_component(database_url, *, amount, source_system):
    """Seed a single CHANNEL DEDUCTION component (used by the net-revenue test)."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            _component(
                kind="DEDUCTION",
                scope_kind="CHANNEL",
                scope_id=CHANNEL,
                amount=amount,
                source_system=source_system,
                key_suffix="net",
            )
        )
        session.commit()


def _seed_extra_component(
    database_url,
    *,
    kind,
    scope_kind,
    scope_id,
    amount,
    source_system,
    key_suffix,
):
    """Seed one extra component row for endpoint filter tests."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            _component(
                kind=kind,
                scope_kind=scope_kind,
                scope_id=scope_id,
                amount=amount,
                source_system=source_system,
                key_suffix=key_suffix,
            )
        )
        session.commit()


def test_finance_viewer_reads_components_grouped_with_audit(tmp_path):
    """Finance viewer with global grants reads all components, grouped by scope,
    with three audit events."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["month"] == MONTH
    assert body["total_count"] == 3
    scopes = {group["scope_kind"]: group for group in body["scopes"]}
    assert set(scopes) == {"CHANNEL", "ACCOUNT", "PAYMENT"}
    assert scopes["CHANNEL"]["component_count"] == 1
    assert scopes["CHANNEL"]["total_amount_usd"] == "120"
    assert scopes["ACCOUNT"]["component_count"] == 1
    assert scopes["ACCOUNT"]["total_amount_usd"] == "70"
    assert scopes["PAYMENT"]["component_count"] == 1
    assert scopes["PAYMENT"]["total_amount_usd"] == "5"
    assert scopes["CHANNEL"]["components"][0]["component_kind"] == "DEDUCTION"
    # raw_payload must never appear anywhere in the response.
    assert "raw_payload" not in str(body)
    assert "secret_provenance" not in str(body)
    assert "LEAK" not in str(body)
    event_types = {e["event_type"] for e in body["audit_events"]}
    assert event_types == {"REVENUE_VIEWED", "PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED"}

    engine = create_engine(database_url)
    with Session(engine) as session:
        from sqlalchemy import select

        logs = session.scalars(select(AuditLogORM)).all()
    assert {log.event_type for log in logs} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
        "BANK_RECONCILIATION_VIEWED",
    }
    assert all(log.sensitive is True for log in logs)


def test_component_kind_filter(tmp_path):
    """Component-kind query param narrows total_count and returned rows to matching kind."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?component_kind=TRANSFER_FEE",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    assert body["total_count"] == 1
    assert body["scopes"][0]["scope_kind"] == "PAYMENT"
    assert body["scopes"][0]["components"][0]["component_kind"] == "TRANSFER_FEE"


def test_scope_kind_filter(tmp_path):
    """Scope-kind query param narrows total_count and returned rows to matching scope."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?scope_kind=CHANNEL",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    assert body["total_count"] == 1
    assert {g["scope_kind"] for g in body["scopes"]} == {"CHANNEL"}


def test_scope_id_filter_and_channel_only_audit(tmp_path):
    """scope_id narrows the SQL result and CHANNEL-only reads emit only revenue audit evidence."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    _seed_extra_component(
        database_url,
        kind="DEDUCTION",
        scope_kind="CHANNEL",
        scope_id="channel-tv-b",
        amount="33.00",
        source_system="adsense_management",
        key_suffix="chan-b",
    )
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?scope_kind=CHANNEL&scope_id={CHANNEL}",
        headers=auth_headers("finance_viewer", "global"),
    ).json()

    assert body["total_count"] == 1
    assert body["returned_count"] == 1
    assert [event["event_type"] for event in body["audit_events"]] == ["REVENUE_VIEWED"]
    scopes = {group["scope_kind"]: group for group in body["scopes"]}
    assert scopes["CHANNEL"]["component_count"] == 1
    assert scopes["CHANNEL"]["total_amount_usd"] == "120"
    assert [c["scope_id"] for c in scopes["CHANNEL"]["components"]] == [CHANNEL]

    engine = create_engine(database_url)
    with Session(engine) as session:
        from sqlalchemy import select

        logs = session.scalars(select(AuditLogORM)).all()
    assert [log.event_type for log in logs] == ["REVENUE_VIEWED"]


def test_pagination_limit_and_offset(tmp_path):
    """Limit+offset pagination returns one row per page while total_count reflects
    the full match set."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?limit=1&offset=0",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    # total_count reflects the full match set; returned rows are paginated.
    assert body["total_count"] == 3
    returned = sum(len(g["components"]) for g in body["scopes"])
    assert returned == 1
    paged_scopes = {group["scope_kind"]: group for group in body["scopes"]}
    assert set(paged_scopes) == {"CHANNEL", "ACCOUNT", "PAYMENT"}
    assert paged_scopes["CHANNEL"]["total_amount_usd"] == "120"
    assert paged_scopes["CHANNEL"]["components"] == []
    # First page of 3 with limit 1 -> more remain; next_offset advances.
    assert body["pagination"] == {
        "limit": 1,
        "offset": 0,
        "next_offset": 1,
        "has_more": True,
    }
    # Last page: offset past the final row -> no more, next_offset is None.
    last = client.get(
        f"/revenue/months/{MONTH}/deduction-components?limit=1&offset=2",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    assert last["pagination"] == {
        "limit": 1,
        "offset": 2,
        "next_offset": None,
        "has_more": False,
    }


def test_deduction_components_openapi_uses_typed_response_model(tmp_path):
    """OpenAPI advertises the concrete response model for deduction-components."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/revenue/months/{month}/deduction-components"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    assert response_schema["$ref"].endswith("/MonthDeductionComponentsResponse")


def test_malformed_month_returns_422(tmp_path):
    """A month value outside 01-12 is rejected with HTTP 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-13/deduction-components",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_net_revenue_endpoint_uses_channel_component_when_net_missing(tmp_path):
    """Net-revenue endpoint derives COMPONENT_DERIVED net from an ADSENSE channel
    component when net is absent."""
    # PR-B integration: a channel whose only fact has net=NULL plus a CHANNEL
    # DEDUCTION component -> /net-revenue derives COMPONENT_DERIVED net.
    # Uses ADSENSE source_kind + adsense_management source_system (distinct from
    # the YOUTUBE_CMS variant already covered in test_net_revenue_api.py).
    database_url = build_database_url(tmp_path)
    seed_database(database_url, net_revenue_usd=None)
    _seed_channel_component(database_url, amount="120.00", source_system="adsense_management")
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/net-revenue",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    channel = response.json()["channels"][0]
    assert channel["status"] == "COMPONENT_DERIVED"
    assert channel["net_revenue_usd"] == "880"  # 1000 - 120, trimmed
    assert channel["confidence"] == "D_ESTIMATED"


def test_assistant_without_view_revenue_is_403(tmp_path):
    """assistant_analyst lacks VIEW_REVENUE and receives HTTP 403 on the
    deduction-components endpoint."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("assistant_analyst", "global"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_finance_admin_global_reads_components(tmp_path):
    """finance_admin with global scope holds all four required permissions and receives HTTP 200."""
    # finance_admin holds all four permissions globally -> 200 (the positive
    # gate companion to the assistant 403). Mirrors the smart-alerts sibling's
    # all-permissions read path; both roles are real in auth/roles.py.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_admin", "global"),
    )
    assert response.status_code == 200


def test_missing_trusted_gateway_token_is_401(tmp_path):
    """A request missing the trusted-gateway token header is rejected with HTTP 401."""
    # Trusted-gateway enforcement runs before route auth; a dropped token -> 401
    # (matches tests/api/test_guarded_routes.py:85-90).
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin", "global")
    headers.pop("x-ums-trusted-gateway-token")
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=headers,
    )
    assert response.status_code == 401


def test_invalid_trusted_gateway_token_is_401(tmp_path):
    """An incorrect trusted-gateway token value is rejected with HTTP 401."""
    # A wrong gateway token -> 401 (matches tests/api/test_database_principals.py:569-573).
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin", "global")
    headers["x-ums-trusted-gateway-token"] = "invalid-token"
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=headers,
    )
    assert response.status_code == 401


def test_company_scoped_finance_viewer_is_403_on_global_revenue_gate(tmp_path):
    """A company-scoped finance_viewer lacks the global VIEW_REVENUE grant and receives HTTP 403."""
    # The endpoint checks VIEW_REVENUE on global_scope() with no org_index (like
    # smart-alerts, revenue.py:679), so it requires a GLOBAL grant. A
    # company-scoped finance_viewer has only a company grant -> first gate fails
    # -> 403 "Missing permission: finance.view_revenue". Confirms the global vs
    # finance-month scope split is enforced, not bypassed by an org-scoped grant.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
