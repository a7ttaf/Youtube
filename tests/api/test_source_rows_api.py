"""Integration tests for the read-only Google source-rows API."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import CurrencyORM, GoogleRevenueSourceRowORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

# Header-based principals carry no tenant_id, so the API resolves the tenant to
# UMS_TENANT_ID. "Own tenant" rows must be seeded under that id; "B" is foreign.
TENANT_A = UUID(UMS_TENANT_ID)
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
USER_ID = UUID("00000000-0000-0000-0000-000000004001")


def auth_headers(role: str) -> dict[str, str]:
    """Build trust-gateway auth headers for the given role at global scope."""
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": "source-rows-user@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    """Return the SQLite URL for an isolated per-test source-rows database."""
    return f"sqlite+pysqlite:///{(tmp_path / 'source_rows.db').as_posix()}"


def _row(
    tenant_id: UUID,
    *,
    month: str = "2026-03",
    source_system: str = "youtube_reporting",
    ingested_at: datetime,
    row_id: UUID | None = None,
) -> GoogleRevenueSourceRowORM:
    """Build one source-row ORM instance for seeding."""
    resolved_id = row_id or uuid4()
    return GoogleRevenueSourceRowORM(
        id=resolved_id,
        tenant_id=tenant_id,
        source_system=source_system,
        source_row_key=resolved_id.hex.ljust(64, "0"),
        source_account_id="acct-1",
        content_owner_id=None,
        youtube_channel_id=None,
        report_type="estimated_revenue",
        report_month=month,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        metric_key="estimated_revenue",
        value_kind="estimated",
        amount_native=Decimal("12.345678"),
        currency_code="USD",
        source_report_id=None,
        raw_file_id=None,
        raw_payload={"secret": "must-not-leak"},
        imported_by=None,
        ingested_at=ingested_at,
    )


def seed_database(database_url: str) -> dict[str, UUID]:
    """Create schema and seed tenants, currency, and two-tenant source rows."""
    engine = create_engine(database_url)
    try:
        TenantBase.metadata.create_all(engine)
        FinanceBase.metadata.create_all(engine)
        base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
        a_id = uuid4()
        b_id = uuid4()
        with Session(engine) as session:
            session.add(TenantORM(id=TENANT_A, slug="tenant-a", display_name="A"))
            session.add(TenantORM(id=TENANT_B, slug="tenant-b", display_name="B"))
            session.add(
                CurrencyORM(
                    code="USD",
                    numeric_code="840",
                    name="US Dollar",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=base,
                )
            )
            session.add_all(
                [
                    _row(TENANT_A, ingested_at=base, row_id=a_id),
                    _row(
                        TENANT_A,
                        source_system="adsense_management",
                        ingested_at=base + timedelta(minutes=1),
                    ),
                    _row(TENANT_A, month="2026-02", ingested_at=base + timedelta(minutes=2)),
                    _row(TENANT_B, ingested_at=base + timedelta(minutes=3), row_id=b_id),
                ]
            )
            session.commit()
        return {"a_row_id": a_id, "b_row_id": b_id}
    finally:
        engine.dispose()


def test_requires_view_revenue(tmp_path):
    """A role without VIEW_REVENUE is rejected with 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        "/revenue/source-rows?month=2026-03", headers=auth_headers("connector_admin")
    )
    assert resp.status_code == 403


def test_lists_only_own_tenant_rows(tmp_path):
    """List returns only own-tenant rows, never raw_payload or tenant_id."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        "/revenue/source-rows?month=2026-03", headers=auth_headers("finance_viewer")
    )
    assert resp.status_code == 200
    body = resp.json()
    # Tenant A has two 2026-03 rows; tenant B's 2026-03 row must be excluded.
    assert len(body["items"]) == 2
    assert all("raw_payload" not in item for item in body["items"])
    assert all("tenant_id" not in item for item in body["items"])
    assert all(item["raw_payload_redacted"] is True for item in body["items"])


def test_source_system_filter_honored(tmp_path):
    """The source_system filter narrows the returned rows."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        "/revenue/source-rows?month=2026-03&source_system=adsense_management",
        headers=auth_headers("finance_viewer"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["source_system"] == "adsense_management"


def test_detail_cross_tenant_is_404(tmp_path):
    """A foreign-tenant row id returns 404, not 403 (no existence leak)."""
    database_url = build_database_url(tmp_path)
    ids = seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        f"/revenue/source-rows/{ids['b_row_id']}",
        headers=auth_headers("finance_viewer"),
    )
    assert resp.status_code == 404


def test_detail_redacts_raw_payload(tmp_path):
    """Own-tenant detail succeeds and never returns raw_payload."""
    database_url = build_database_url(tmp_path)
    ids = seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        f"/revenue/source-rows/{ids['a_row_id']}",
        headers=auth_headers("finance_viewer"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "raw_payload" not in body
    assert "tenant_id" not in body
    assert body["raw_payload_redacted"] is True


def test_half_cursor_is_422(tmp_path):
    """Providing only one half of the keyset cursor is rejected with 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    resp = client.get(
        "/revenue/source-rows?month=2026-03&cursor_id=" + str(uuid4()),
        headers=auth_headers("finance_viewer"),
    )
    assert resp.status_code == 422
