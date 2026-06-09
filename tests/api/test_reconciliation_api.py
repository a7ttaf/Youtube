from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    ContentOwnerChannelLinkORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

SECTOR_ID = UUID("00000000-0000-0000-0000-0000000f1101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000f1201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000f1301")
FINANCE_USER_ID = UUID("00000000-0000-0000-0000-0000000f1401")
CREATOR_ID = UUID("00000000-0000-0000-0000-0000000f1402")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-0000000f9001")

MONTH = "2026-03"
CHANNEL = "channel-recon-a"


def build_database_url(tmp_path) -> str:
    """Return a SQLite database URL rooted at the pytest tmp_path directory."""
    return f"sqlite+pysqlite:///{(tmp_path / 'reconciliation.db').as_posix()}"


def seed_database(database_url: str, *, locked: bool = False) -> None:
    """Create org/security/finance/explanation/tenant tables and seed a month."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ExplanationBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                TenantORM(
                    id=UUID(UMS_TENANT_ID),
                    slug="ums",
                    display_name="UMS",
                    primary_currency="USD",
                    status="ACTIVE",
                ),
                OrgUnitORM(
                    id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY",
                    name="TV Company", active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id=CHANNEL,
                    channel_name="Recon A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(
                    id=FINANCE_USER_ID,
                    email="recon-finance@example.com",
                    display_name="Recon Finance",
                ),
                UserORM(
                    id=CREATOR_ID,
                    email="recon-admin@example.com",
                    display_name="Recon Admin",
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month=MONTH,
                    youtube_channel_id=CHANNEL,
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-2026-03",
                    gross_revenue_usd=Decimal("1000.00"),
                    net_revenue_usd=None,
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=CREATOR_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month=MONTH,
                    payment_name="pub-1-2026-03",
                    source_account_id="pub-1",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("800.00"),
                    payment_currency="USD",
                    payment_status="PAID",
                ),
                AdsenseContentOwnerLinkORM(
                    id=uuid4(),
                    adsense_account_id="pub-1",
                    content_owner_id="owner-recon-a",
                    verification_status="VERIFIED",
                    provenance_kind="MANUAL",
                    provenance_payload={},
                    effective_month_start=MONTH,
                    effective_month_end=None,
                ),
                ContentOwnerChannelLinkORM(
                    id=uuid4(),
                    content_owner_id="owner-recon-a",
                    youtube_channel_id=CHANNEL,
                    provenance_kind="MANUAL",
                    active=True,
                    effective_month_start=MONTH,
                    effective_month_end=None,
                ),
                BankReconciliationEntryORM(
                    id=uuid4(),
                    month=MONTH,
                    bank_reference="wire-1",
                    bank_received_date=date(2026, 4, 25),
                    bank_received_amount=Decimal("760.00"),
                    bank_received_currency="USD",
                    bank_received_amount_usd=Decimal("760.00"),
                    fx_difference_usd=Decimal("10.00"),
                    recorded_by=CREATOR_ID,
                ),
            ]
        )
        if locked:
            session.add(
                FinanceMonthCloseORM(
                    tenant_id=UUID(UMS_TENANT_ID),
                    month=MONTH,
                    status="LOCKED",
                    allocation_rule_payload={},
                )
            )
        session.commit()


def _principal(*grants: PermissionGrant) -> UserPrincipal:
    """Return a finance principal with explicit direct permission grants."""
    return UserPrincipal(
        user_id=str(FINANCE_USER_ID),
        email="recon-finance@example.com",
        direct_permissions=tuple(grants),
    )


def _reconcile_principal() -> UserPrincipal:
    """Principal allowed to reconcile and read the resulting explanation."""
    return _principal(
        PermissionGrant(
            Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(MONTH)
        ),
        PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
        PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()),
        PermissionGrant(
            Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(MONTH)
        ),
    )


def _with_principal(client, principal: UserPrincipal):
    """Install a principal override on the test client app."""
    client.app.dependency_overrides[current_principal_from_headers] = (
        lambda: principal
    )


def _clear_principal(client) -> None:
    """Remove the principal override from the test client app."""
    client.app.dependency_overrides.pop(current_principal_from_headers, None)


def test_reconcile_returns_per_channel_lines_and_totals(tmp_path):
    """POST reconcile returns 200 with per-channel lines, totals, and warnings."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == MONTH
    lines = body["channels"]
    assert len(lines) == 1
    line = lines[0]
    assert line["youtube_channel_id"] == CHANNEL
    assert line["gross_usd"] == "1000"
    assert "net_received_usd" in line
    assert "totals" in body
    assert body["totals"]["gross_total_usd"] == "1000"
    assert isinstance(body["warnings"], list)


def test_reconcile_missing_permission_is_forbidden(tmp_path):
    """A principal without CHANGE_ALLOCATION_RULE gets 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(
        client,
        _principal(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope())
        ),
    )
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: finance.change_allocation_rule"
    )


def test_reconcile_requires_revenue_read_permission(tmp_path):
    """POST reconcile requires revenue read access before exposing totals."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(
        client,
        _principal(
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(MONTH)
            ),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(MONTH)
            ),
        ),
    )
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_reconcile_requires_payment_read_permission(tmp_path):
    """POST reconcile requires finalized payment read access for payment-derived data."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(
        client,
        _principal(
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(MONTH)
            ),
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
        ),
    )
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: finance.view_finalized_payments"
    )


def test_reconcile_locked_month_conflicts(tmp_path):
    """Reconciling a LOCKED month returns 409."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked=True)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 409


def test_reconcile_bad_month_is_unprocessable(tmp_path):
    """A malformed month path segment returns 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.post(
            "/revenue/months/2026-3/reconcile",
            json={"reason": "monthly close"},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 422


def test_reconcile_blank_reason_is_unprocessable(tmp_path):
    """Whitespace-only audit reasons are rejected before reconciliation."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "   "},
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 422


def test_get_reconciliation_returns_persisted_explanation(tmp_path):
    """GET reconciliation returns the persisted explanation and audits the read."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        client.post(
            f"/revenue/months/{MONTH}/reconcile",
            json={"reason": "monthly close"},
        )
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == CHANNEL
    assert body["metric"] == "revenue_reconciliation_usd"
    assert body["currency"] == "USD"
    assert isinstance(body["components"], list)
    assert [event["event_type"] for event in body["audit_events"]] == [
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    ]

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(
            select(AuditLogORM).where(
                AuditLogORM.event_type.in_(("REVENUE_VIEWED", "PAYMENT_VIEWED"))
            )
        ).all()
    audit_by_type = {log.event_type: log for log in audit_logs}
    assert set(audit_by_type) == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    assert audit_by_type["REVENUE_VIEWED"].scope_type == "channel"
    assert audit_by_type["REVENUE_VIEWED"].scope_id == CHANNEL
    assert audit_by_type["PAYMENT_VIEWED"].scope_type == "finance-month"
    assert audit_by_type["PAYMENT_VIEWED"].scope_id == MONTH
    assert all(log.sensitive is True for log in audit_logs)


def test_get_reconciliation_missing_is_not_found(tmp_path):
    """GET reconciliation with no persisted explanation returns 404."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 404


def test_get_reconciliation_cross_tenant_isolation(tmp_path):
    """An explanation persisted under another tenant is not visible (404)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    # Persist a reconciliation explanation under a DIFFERENT tenant only.
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            NumberExplanationORM(
                id=uuid4(),
                tenant_id=OTHER_TENANT_ID,
                month=MONTH,
                entity_type="channel",
                entity_id=CHANNEL,
                metric="revenue_reconciliation_usd",
                value=Decimal("123.00"),
                currency="USD",
                formula="x",
                confidence="MEDIUM",
                components=[],
                warnings=[],
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _reconcile_principal())
    try:
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 404


def test_get_reconciliation_missing_permission_is_forbidden(tmp_path):
    """A principal without VIEW_REVENUE on the channel gets 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(client, _principal())
    try:
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403


def test_get_reconciliation_requires_confidence_permission(tmp_path):
    """GET explanations require confidence access, not revenue alone."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(
        client,
        _principal(PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope())),
    )
    try:
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: analytics.view_confidence"
    )


def test_get_reconciliation_requires_payment_permission(tmp_path):
    """GET explanations require finalized-payment access for payment provenance."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _with_principal(
        client,
        _principal(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()),
        ),
    )
    try:
        response = client.get(
            f"/revenue/channels/{CHANNEL}/months/{MONTH}/reconciliation",
        )
    finally:
        _clear_principal(client)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: finance.view_finalized_payments"
    )
