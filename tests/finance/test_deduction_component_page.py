"""Repository-layer filtering + pagination for deduction components (SQLite)."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import DeductionComponentORM, FinanceBase
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    """Create a fresh SQLite engine with FinanceBase tables for one test run."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def _add(session, *, kind, scope_kind, scope_id, key):
    """Insert one DeductionComponentORM row with fixed USD amounts into the session."""
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=TENANT,
            month=MONTH,
            component_kind=kind,
            scope_kind=scope_kind,
            scope_id=scope_id,
            amount_usd=Decimal("10.00"),
            amount_native=None,
            currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            source_id=None,
            source_key=key,
            source_report_id=None,
            raw_payload={"k": "v"},
            component_key=key,
        )
    )


def _seed(session):
    """Seed one CHANNEL, ACCOUNT, and PAYMENT component covering all three scope kinds."""
    _add(session, kind="DEDUCTION", scope_kind="CHANNEL", scope_id="chan-1", key="k-chan")
    _add(
        session, kind="UNRESOLVED_PAYMENT_GAP", scope_kind="ACCOUNT", scope_id="pub-1", key="k-acct"
    )
    _add(session, kind="TRANSFER_FEE", scope_kind="PAYMENT", scope_id="BANK-1", key="k-pay")
    session.commit()


def test_page_returns_all_with_total_when_unfiltered(tmp_path):
    """Unfiltered page returns all three rows with total_count == 3 in deterministic order."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(month=MONTH, limit=100, offset=0)
    assert page.total_count == 3
    assert len(page.components) == 3
    assert [(t.scope_kind, t.component_count, t.total_amount_usd) for t in page.scope_totals] == [
        ("ACCOUNT", 1, Decimal("10.00")),
        ("CHANNEL", 1, Decimal("10.00")),
        ("PAYMENT", 1, Decimal("10.00")),
    ]
    # deterministic order: scope_kind, scope_id, component_kind, component_key
    assert [c.scope_kind for c in page.components] == ["ACCOUNT", "CHANNEL", "PAYMENT"]


def test_page_component_kind_filter_counts_only_matches(tmp_path):
    """component_kind filter returns only matching rows and sets total_count to the
    filtered count."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(
            month=MONTH, component_kind="TRANSFER_FEE", limit=100, offset=0
        )
    assert page.total_count == 1
    assert page.components[0].component_kind == "TRANSFER_FEE"


def test_page_scope_kind_filter_counts_only_matches(tmp_path):
    """scope_kind filter returns only matching rows and sets total_count to the filtered count."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(
            month=MONTH, scope_kind="CHANNEL", limit=100, offset=0
        )
    assert page.total_count == 1
    assert page.components[0].scope_kind == "CHANNEL"
    assert [(t.scope_kind, t.component_count) for t in page.scope_totals] == [("CHANNEL", 1)]


def test_page_scope_id_filter_counts_only_matches(tmp_path):
    """scope_id filter returns only the requested scope id and counts the filtered set."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        _add(session, kind="DEDUCTION", scope_kind="CHANNEL", scope_id="chan-2", key="k-chan-2")
        session.commit()
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(
            month=MONTH,
            scope_kind="CHANNEL",
            scope_id="chan-1",
            limit=100,
            offset=0,
        )
    assert page.total_count == 1
    assert [component.scope_id for component in page.components] == ["chan-1"]
    assert [(t.scope_kind, t.component_count) for t in page.scope_totals] == [("CHANNEL", 1)]


def test_page_limit_offset_paginates_but_total_is_full_match_count(tmp_path):
    """limit/offset slices the returned rows while total_count always reflects the
    full filtered set."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(month=MONTH, limit=1, offset=0)
        page2 = repo.list_month_components_page(month=MONTH, limit=1, offset=2)
    assert page.total_count == 3
    assert len(page.components) == 1
    assert [(t.scope_kind, t.component_count) for t in page.scope_totals] == [
        ("ACCOUNT", 1),
        ("CHANNEL", 1),
        ("PAYMENT", 1),
    ]
    assert page.components[0].scope_kind == "ACCOUNT"  # first in deterministic order
    assert page2.components[0].scope_kind == "PAYMENT"  # third


def test_list_month_components_filters_component_kinds_in_sql(tmp_path):
    """list_month_components can restrict CHANNEL reads to net-applicable component kinds."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        _add(session, kind="TRANSFER_FEE", scope_kind="CHANNEL", scope_id="chan-1", key="k-fee")
        session.commit()
        repo = SqlAlchemyDeductionComponentRepository(session)
        components = repo.list_month_components(
            month=MONTH,
            youtube_channel_ids={"chan-1"},
            component_kinds={"TAX", "DEDUCTION"},
        )
    assert [component.component_kind for component in components] == ["DEDUCTION"]


def test_page_malformed_month_raises(tmp_path):
    """A month value with an invalid calendar month raises DeductionComponentValidationError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(DeductionComponentValidationError):
            repo.list_month_components_page(month="2026-13", limit=100, offset=0)


def test_page_rejects_non_positive_limit(tmp_path):
    """A limit below 1 fails closed with DeductionComponentValidationError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(DeductionComponentValidationError, match="limit must be >= 1"):
            repo.list_month_components_page(month=MONTH, limit=0, offset=0)


def test_page_rejects_negative_offset(tmp_path):
    """A negative offset fails closed with DeductionComponentValidationError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(DeductionComponentValidationError, match="offset must be >= 0"):
            repo.list_month_components_page(month=MONTH, limit=10, offset=-1)
