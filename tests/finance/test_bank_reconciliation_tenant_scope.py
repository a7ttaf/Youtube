"""Tenant-bound bank reconciliation repository behaviour tests.

These tests prove that ``SqlAlchemyBankReconciliationRepository``:

- stamps the resolved ``tenant_id`` on every inserted/upserted row,
- filters every read to the resolved ``tenant_id``,
- honours the documented precedence: explicit constructor argument >
  ambient ``TENANT_CTX`` > UMS default tenant,
- preserves per-tenant composite uniqueness on
  ``(tenant_id, month, bank_reference)``, and
- isolates the ``_require_month_open`` lock check to the bound tenant
  (a locked month in tenant A does not block writes in tenant B).

The audit, user, and AdSense payment tenant-scope suites established
this pattern; this file mirrors it for the bank reconciliation surface,
which was wired in S2.4a (PR #21) but was only indirectly exercised
through API and month-close integration tests until now.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    BankReconciliationEntryORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationLockedMonthError,
    BankReconciliationValidationError,
    SqlAlchemyBankReconciliationRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
SECOND_TENANT_ID = UUID("00000000-0000-0000-0000-000000041999")
ACTOR_USER_ID = "00000000-0000-0000-0000-000000041001"
CREATED_AT = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)


def build_session() -> Session:
    """Create an isolated in-memory finance schema for tenant-scope tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _tenant(tenant_id: UUID, *, slug: str) -> Tenant:
    """Build an active tenant context object for request-scope assertions."""
    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=f"{slug.title()} Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=CREATED_AT,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _record_entry_kwargs(
    *,
    bank_reference: str,
    month: str = "2026-03",
    amount: str = "928.50",
) -> dict[str, object]:
    """Return a canonical record_entry kwargs dict with overridable fields."""
    return {
        "month": month,
        "bank_reference": bank_reference,
        "bank_received_date": date(2026, 4, 22),
        "bank_received_amount": Decimal(amount),
        "bank_received_currency": "USD",
        "bank_received_amount_usd": Decimal(amount),
        "transfer_fee_usd": Decimal("1.50"),
        "fx_difference_usd": Decimal("0"),
        "notes": "Bank transfer received",
        "source_report_id": None,
        "actor_user_id": ACTOR_USER_ID,
    }


def _seed_entry(
    session: Session,
    *,
    tenant_id: UUID,
    bank_reference: str,
    month: str = "2026-03",
) -> UUID:
    """Insert a bank reconciliation row for a specific tenant and return its id."""
    row_id = uuid4()
    session.add(
        BankReconciliationEntryORM(
            id=row_id,
            tenant_id=tenant_id,
            month=month,
            bank_reference=bank_reference,
            bank_received_date=date(2026, 4, 22),
            bank_received_amount=Decimal("100.00"),
            bank_received_currency="USD",
            bank_received_amount_usd=Decimal("100.00"),
            transfer_fee_usd=Decimal("0"),
            fx_difference_usd=Decimal("0"),
            notes=None,
            source_report_id=None,
            recorded_by=UUID(ACTOR_USER_ID),
        )
    )
    session.commit()
    return row_id


# ---------------------------------------------------------------------------
# record_entry / writes
# ---------------------------------------------------------------------------


def test_record_entry_stamps_default_tenant_without_context() -> None:
    """Bootstrap callers fall back to the UMS default tenant on writes."""
    session = build_session()

    SqlAlchemyBankReconciliationRepository(session).record_entry(
        **_record_entry_kwargs(bank_reference="default-tenant-bank-ref")
    )

    row = session.scalars(select(BankReconciliationEntryORM)).one()
    assert row.tenant_id == DEFAULT_TENANT_ID
    assert row.bank_reference == "default-tenant-bank-ref"


def test_record_entry_stamps_explicit_constructor_tenant() -> None:
    """Explicit constructor tenant ids are stamped onto every inserted row."""
    session = build_session()

    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).record_entry(**_record_entry_kwargs(bank_reference="second-tenant-bank-ref"))

    row = session.scalars(select(BankReconciliationEntryORM)).one()
    assert row.tenant_id == SECOND_TENANT_ID


def test_record_entry_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes writes before explicit DI wiring lands."""
    session = build_session()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        SqlAlchemyBankReconciliationRepository(session).record_entry(
            **_record_entry_kwargs(bank_reference="ctx-tenant-bank-ref")
        )
    finally:
        TENANT_CTX.reset(token)

    row = session.scalars(select(BankReconciliationEntryORM)).one()
    assert row.tenant_id == SECOND_TENANT_ID


def test_record_entry_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on writes."""
    session = build_session()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        SqlAlchemyBankReconciliationRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).record_entry(**_record_entry_kwargs(bank_reference="explicit-wins-bank-ref"))
    finally:
        TENANT_CTX.reset(token)

    row = session.scalars(select(BankReconciliationEntryORM)).one()
    assert row.tenant_id == DEFAULT_TENANT_ID


def test_record_entry_allows_same_bank_reference_in_two_tenants() -> None:
    """Per-tenant composite uniqueness lets two tenants share month + reference."""
    session = build_session()

    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).record_entry(
        **_record_entry_kwargs(bank_reference="bank-2026-03", amount="100.00")
    )
    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).record_entry(
        **_record_entry_kwargs(bank_reference="bank-2026-03", amount="200.00")
    )

    rows = session.scalars(select(BankReconciliationEntryORM)).all()
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {DEFAULT_TENANT_ID, SECOND_TENANT_ID}

    amounts = {row.tenant_id: row.bank_received_amount for row in rows}
    assert amounts[DEFAULT_TENANT_ID] == Decimal("100.00")
    assert amounts[SECOND_TENANT_ID] == Decimal("200.00")


def test_record_entry_upsert_is_scoped_to_one_tenant() -> None:
    """Re-upserting in tenant A leaves tenant B's row with the same reference alone."""
    session = build_session()

    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).record_entry(
        **_record_entry_kwargs(bank_reference="upsert-bank-ref", amount="100.00")
    )
    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).record_entry(
        **_record_entry_kwargs(bank_reference="upsert-bank-ref", amount="200.00")
    )
    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).record_entry(
        **_record_entry_kwargs(bank_reference="upsert-bank-ref", amount="111.00")
    )

    rows = session.scalars(select(BankReconciliationEntryORM)).all()
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {DEFAULT_TENANT_ID, SECOND_TENANT_ID}

    amounts = {row.tenant_id: row.bank_received_amount for row in rows}
    assert amounts[DEFAULT_TENANT_ID] == Decimal("111.00")
    assert amounts[SECOND_TENANT_ID] == Decimal("200.00")


def test_record_entry_locked_month_is_scoped_to_the_bound_tenant() -> None:
    """A locked finance month in tenant A does not block writes in tenant B."""
    session = build_session()
    session.add(
        FinanceMonthCloseORM(
            tenant_id=DEFAULT_TENANT_ID,
            month="2026-03",
            status="LOCKED",
            locked_by=UUID(ACTOR_USER_ID),
            allocation_rule_payload={},
        )
    )
    session.commit()

    with pytest.raises(BankReconciliationLockedMonthError):
        SqlAlchemyBankReconciliationRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).record_entry(**_record_entry_kwargs(bank_reference="locked-month-bank-ref"))

    SqlAlchemyBankReconciliationRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).record_entry(**_record_entry_kwargs(bank_reference="open-month-bank-ref"))

    rows = session.scalars(select(BankReconciliationEntryORM)).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == SECOND_TENANT_ID
    assert rows[0].bank_reference == "open-month-bank-ref"


# ---------------------------------------------------------------------------
# list_month_entries / reads
# ---------------------------------------------------------------------------


def test_list_month_entries_filters_to_explicit_tenant_id() -> None:
    """Explicit tenant injection isolates list_month_entries reads."""
    session = build_session()
    _seed_entry(session, tenant_id=DEFAULT_TENANT_ID, bank_reference="default-bank-ref")
    _seed_entry(session, tenant_id=SECOND_TENANT_ID, bank_reference="second-bank-ref")

    entries = SqlAlchemyBankReconciliationRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).list_month_entries(month="2026-03")

    assert [entry.bank_reference for entry in entries] == ["second-bank-ref"]


def test_list_month_entries_returns_empty_for_empty_tenant() -> None:
    """Tenant filters return an empty list instead of leaking rows."""
    session = build_session()
    _seed_entry(session, tenant_id=DEFAULT_TENANT_ID, bank_reference="default-bank-ref")
    _seed_entry(session, tenant_id=SECOND_TENANT_ID, bank_reference="second-bank-ref")

    entries = SqlAlchemyBankReconciliationRepository(
        session, tenant_id=uuid4()
    ).list_month_entries(month="2026-03")

    assert entries == []


def test_list_month_entries_uses_default_tenant_without_context() -> None:
    """Bootstrap callers see only UMS-default rows on reads."""
    session = build_session()
    _seed_entry(session, tenant_id=DEFAULT_TENANT_ID, bank_reference="default-bank-ref")
    _seed_entry(session, tenant_id=SECOND_TENANT_ID, bank_reference="second-bank-ref")

    entries = SqlAlchemyBankReconciliationRepository(session).list_month_entries(
        month="2026-03"
    )

    assert [entry.bank_reference for entry in entries] == ["default-bank-ref"]


def test_list_month_entries_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes reads when no explicit tenant is supplied."""
    session = build_session()
    _seed_entry(session, tenant_id=DEFAULT_TENANT_ID, bank_reference="default-bank-ref")
    _seed_entry(session, tenant_id=SECOND_TENANT_ID, bank_reference="second-bank-ref")
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        entries = SqlAlchemyBankReconciliationRepository(session).list_month_entries(
            month="2026-03"
        )
    finally:
        TENANT_CTX.reset(token)

    assert [entry.bank_reference for entry in entries] == ["second-bank-ref"]


def test_list_month_entries_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on reads."""
    session = build_session()
    _seed_entry(session, tenant_id=DEFAULT_TENANT_ID, bank_reference="default-bank-ref")
    _seed_entry(session, tenant_id=SECOND_TENANT_ID, bank_reference="second-bank-ref")
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        entries = SqlAlchemyBankReconciliationRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).list_month_entries(month="2026-03")
    finally:
        TENANT_CTX.reset(token)

    assert [entry.bank_reference for entry in entries] == ["default-bank-ref"]


# ---------------------------------------------------------------------------
# constructor input validation
# ---------------------------------------------------------------------------


def test_bank_reconciliation_repository_rejects_invalid_tenant_id_string() -> None:
    """Malformed constructor tenant ids fail closed before any DB access."""
    session = build_session()

    with pytest.raises(
        BankReconciliationValidationError, match="tenant_id must be a valid UUID"
    ):
        SqlAlchemyBankReconciliationRepository(session, tenant_id="not-a-uuid")
