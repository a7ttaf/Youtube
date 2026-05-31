from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)
USER_ID = UUID("00000000-0000-0000-0000-0000000d0501")


def auth_headers(role, scope_type="global", scope_id=None):
    """Trusted-gateway identity headers for a role/scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "alloc@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url, *, add_unmapped=False, add_payment=False):
    """Create schema + one verified-map account with gross and a deduction."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="alloc@example.com", display_name="Alloc"))
        session.add(
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
                channel_name="Channel A", active=True,
            )
        )
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
                content_owner_id="owner-1", verification_status="VERIFIED",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
                effective_month_start="2026-01",
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            )
        )
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                youtube_channel_id="chA", source_kind="ADSENSE",
                gross_revenue_usd=Decimal("500.00"),
            )
        )
        session.add(
            DeductionComponentORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-1",
                amount_usd=Decimal("100.00"), currency_code="USD",
                source_system="adsense_management",
                source_table="google_revenue_source_rows",
                component_key="acct-ded-1", raw_payload={},
            )
        )
        if add_unmapped:
            session.add(
                DeductionComponentORM(
                    id=uuid4(), tenant_id=TENANT, month=MONTH,
                    component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-x",
                    amount_usd=Decimal("9.00"), currency_code="USD",
                    source_system="adsense_management",
                    source_table="google_revenue_source_rows",
                    component_key="acct-ded-x", raw_payload={},
                )
            )
        if add_payment:
            session.add(
                DeductionComponentORM(
                    id=uuid4(), tenant_id=TENANT, month=MONTH,
                    component_kind="TRANSFER_FEE", scope_kind="PAYMENT",
                    scope_id="BANK-1", amount_usd=Decimal("2.50"),
                    currency_code="USD", source_system="bank_reconciliation",
                    source_table="bank_reconciliation_entries",
                    component_key="pay-fee-1", raw_payload={},
                )
            )
        session.commit()


def test_finance_viewer_gets_allocation(tmp_path):
    """finance_viewer sees the single-channel allocation + both view audits."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allocation_method"] == "gross_revenue_proportional"
    assert len(body["allocations"]) == 1
    line = body["allocations"][0]
    assert line["adsense_account_id"] == "pub-1"
    assert line["youtube_channel_id"] == "chA"
    assert line["allocated_amount_usd"] == "100"
    assert line["net_applicable"] is True
    assert body["summary"]["allocated_total_usd"] == "100"
    assert {e["event_type"] for e in body["audit_events"]} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = {log.event_type for log in session.scalars(select(AuditLogORM)).all()}
    assert logs == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_unmapped_account_reports_blocking_issue(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url, add_unmapped=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    codes = {iss["issue_code"] for iss in body["unallocated"]}
    assert "ACCOUNT_UNMAPPED_OR_UNVERIFIED" in codes


def test_account_filter_narrows_results(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url, add_unmapped=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        params={"adsense_account_id": "pub-1"},
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["unallocated"] == []
    assert len(body["allocations"]) == 1


def test_missing_finance_view_is_forbidden(tmp_path):
    """corporate_admin lacks finance-view permissions -> 403 (fail-closed)."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("corporate_admin", "global"),
    )
    assert response.status_code == 403


def test_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-13/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_finance_month_scope_is_rejected_for_global_read(tmp_path):
    """A finance-month-scoped grant cannot satisfy the VIEW_REVENUE@global target."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "finance-month", MONTH),
    )
    assert response.status_code == 403


def test_payment_grain_excluded_and_no_bank_audit(tmp_path):
    """A PAYMENT-grain component is never fetched/returned and emits no bank audit."""
    database_url = build_database_url(tmp_path)
    seed(database_url, add_payment=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    surfaced = {ln["adsense_account_id"] for ln in body["allocations"]} | {
        iss["scope_id"] for iss in body["unallocated"]
    }
    assert "BANK-1" not in surfaced  # PAYMENT-grain never fetched or surfaced
    assert all(ln["youtube_channel_id"] == "chA" for ln in body["allocations"])
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = {log.event_type for log in session.scalars(select(AuditLogORM)).all()}
    assert "BANK_RECONCILIATION_VIEWED" not in logs
    assert logs == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
