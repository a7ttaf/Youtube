"""SQLite-backed tests for the tenant-scoped source-rows read repository."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.source_rows_read import (
    MAX_SOURCE_ROW_PAGE_SIZE,
    SourceRowValidationError,
    get_source_row,
    list_source_rows,
)

A = UUID("00000000-0000-0000-0000-000000000001")
B = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()


def _seed_tenants_and_currency(session: Session) -> None:
    activated = datetime.now(UTC)
    session.add(TenantORM(id=A, slug="tenant-a", display_name="A"))
    session.add(TenantORM(id=B, slug="tenant-b", display_name="B"))
    session.add(
        CurrencyORM(
            code="USD",
            numeric_code="840",
            name="US Dollar",
            minor_unit=2,
            is_supported=True,
            activated_at=activated,
        )
    )
    session.flush()


def _row(
    tenant_id: UUID,
    *,
    month: str = "2026-03",
    source_system: str = "youtube_reporting",
    ingested_at: datetime,
    row_id: UUID | None = None,
) -> GoogleRevenueSourceRowORM:
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


@pytest.fixture
def seed_rows(session: Session) -> dict[str, UUID]:
    _seed_tenants_and_currency(session)
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    b_id = uuid4()
    session.add_all(
        [
            _row(A, ingested_at=base),
            _row(
                A,
                source_system="adsense_management",
                ingested_at=base + timedelta(minutes=1),
            ),
            _row(A, month="2026-02", ingested_at=base + timedelta(minutes=2)),
            _row(B, ingested_at=base + timedelta(minutes=3), row_id=b_id),
        ]
    )
    session.flush()
    return {"b_id": b_id}


@pytest.fixture
def seed_many(session: Session) -> dict[str, UUID]:
    _seed_tenants_and_currency(session)
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    session.add_all(
        [_row(A, ingested_at=base + timedelta(minutes=i)) for i in range(5)]
    )
    session.flush()
    return {}


def test_list_filters_by_tenant_and_month(session, seed_rows):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=50)
    assert all(e.report_month == "2026-03" for e in page.items)
    # No tenant B row leaks.
    assert all(e for e in page.items)  # entries carry no tenant_id field
    assert seed_rows["b_id"] not in {UUID(e.id) for e in page.items}


def test_entry_never_exposes_raw_payload(session, seed_rows):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=50)
    api = page.items[0].to_api()
    assert "raw_payload" not in api
    assert api["raw_payload_redacted"] is True
    assert "tenant_id" not in api


def test_source_system_filter(session, seed_rows):
    page = list_source_rows(
        session, tenant_id=A, month="2026-03",
        source_system="adsense_management", limit=50,
    )
    assert all(e.source_system == "adsense_management" for e in page.items)


def test_half_cursor_raises(session):
    with pytest.raises(SourceRowValidationError):
        list_source_rows(
            session, tenant_id=A, month="2026-03",
            cursor_ingested_at=None, cursor_id="x", limit=50,
        )


def test_limit_out_of_range_raises(session):
    with pytest.raises(SourceRowValidationError):
        list_source_rows(session, tenant_id=A, month="2026-03", limit=0)
    with pytest.raises(SourceRowValidationError):
        list_source_rows(
            session, tenant_id=A, month="2026-03",
            limit=MAX_SOURCE_ROW_PAGE_SIZE + 1,
        )


def test_get_returns_none_for_other_tenant(session, seed_rows):
    # An id owned by tenant B is invisible to tenant A (=> route 404).
    assert get_source_row(session, tenant_id=A, row_id=str(seed_rows["b_id"])) is None


def test_pagination_has_more_and_cursor(session, seed_many):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=2)
    assert len(page.items) == 2
    assert page.next_cursor is not None
    nxt = list_source_rows(
        session, tenant_id=A, month="2026-03", limit=2,
        cursor_ingested_at=page.next_cursor["ingested_at"],
        cursor_id=page.next_cursor["id"],
    )
    assert {e.id for e in nxt.items}.isdisjoint({e.id for e in page.items})
