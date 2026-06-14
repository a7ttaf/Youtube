from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.finance.month_close import (
    get_month_close_status,
    get_or_create_month_close_row,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000031001")


def build_session() -> Session:
    """Create an isolated in-memory finance schema for month-status tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _lock_month(session: Session, month: str, *, tenant_id: UUID) -> None:
    """Mark a month locked directly for the read-only status accessor tests."""
    # Drive the close row straight to LOCKED for a read-only accessor test,
    # bypassing the readiness-gated lock_month() machinery on purpose.
    row = get_or_create_month_close_row(session, month, tenant_id=tenant_id, for_update=False)
    row.status = "LOCKED"
    session.flush()


def test_status_none_when_no_row() -> None:
    """Missing month-close rows return None without creating rows."""
    session = build_session()
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) is None
    # Read-only: the accessor must NOT have created a close row.
    assert session.scalars(select(FinanceMonthCloseORM)).all() == []


def test_status_open_when_row_open() -> None:
    """Existing OPEN rows return OPEN."""
    session = build_session()
    get_or_create_month_close_row(session, "2026-04", tenant_id=TENANT_ID, for_update=False)
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) == "OPEN"


def test_status_reflects_locked() -> None:
    """Existing LOCKED rows return LOCKED."""
    session = build_session()
    _lock_month(session, "2026-04", tenant_id=TENANT_ID)
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) == "LOCKED"
