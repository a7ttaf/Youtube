"""Tenant-bound AdSense payment repository behaviour tests.

These tests prove that ``SqlAlchemyAdSensePaymentRepository``:

- stamps the resolved ``tenant_id`` on every inserted/upserted row,
- filters every read to the resolved ``tenant_id``, and
- honours the documented precedence: explicit constructor argument >
  ambient ``TENANT_CTX`` > UMS default tenant.

The audit and user-account tenant-scope suites established this pattern;
this file mirrors it for the AdSense payment surface, which was wired in
PR #21 (S2.4a) but was only indirectly exercised through API and
month-close integration tests until now.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import AdSensePaymentORM, FinanceBase
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentInput,
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
SECOND_TENANT_ID = UUID("00000000-0000-0000-0000-000000031999")
ACTOR_USER_ID = "00000000-0000-0000-0000-000000031001"
CREATED_AT = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)


def build_session() -> Session:
    """Create an isolated in-memory finance schema for tenant-scope tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _payment_input(
    *, payment_name: str, amount: str = "1234.56"
) -> AdSensePaymentInput:
    """Build a default AdSense payment input for tenant-scope write tests."""
    return AdSensePaymentInput(
        source_account_id="pub-1",
        month="2026-03",
        payment_name=payment_name,
        payment_date=date(2026, 4, 15),
        payment_amount=Decimal(amount),
        payment_currency="USD",
        payment_status="PAID",
        raw_payload={"paymentId": payment_name},
    )


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


def _seed_payment(
    session: Session,
    *,
    tenant_id: UUID,
    payment_name: str,
    month: str = "2026-03",
    source_account_id: str = "pub-1",
    payment_id: UUID | None = None,
) -> UUID:
    """Insert a paid payment for a specific tenant and return the row id."""
    payment_id = payment_id or uuid4()
    session.add(
        AdSensePaymentORM(
            id=payment_id,
            tenant_id=tenant_id,
            month=month,
            payment_name=payment_name,
            source_account_id=source_account_id,
            payment_date=date(2026, 4, 15),
            payment_amount=Decimal("100.00"),
            payment_currency="USD",
            payment_status="PAID",
            raw_payload={"paymentId": payment_name},
            source_report_id=None,
            imported_by=UUID(ACTOR_USER_ID),
        )
    )
    session.commit()
    return payment_id


# ---------------------------------------------------------------------------
# sync_payments / writes
# ---------------------------------------------------------------------------


def test_sync_payments_stamps_default_tenant_without_context() -> None:
    """Bootstrap callers fall back to the UMS default tenant on writes."""
    session = build_session()

    SqlAlchemyAdSensePaymentRepository(session).sync_payments(
        payments=[_payment_input(payment_name="Default Tenant Payment")],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )

    row = session.scalars(select(AdSensePaymentORM)).one()
    assert row.tenant_id == DEFAULT_TENANT_ID
    assert row.payment_name == "Default Tenant Payment"


def test_sync_payments_stamps_explicit_constructor_tenant() -> None:
    """Explicit constructor tenant ids are stamped onto every inserted row."""
    session = build_session()

    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).sync_payments(
        payments=[_payment_input(payment_name="Second Tenant Payment")],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )

    row = session.scalars(select(AdSensePaymentORM)).one()
    assert row.tenant_id == SECOND_TENANT_ID


def test_sync_payments_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes writes before explicit DI wiring lands."""
    session = build_session()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        SqlAlchemyAdSensePaymentRepository(session).sync_payments(
            payments=[_payment_input(payment_name="Context Tenant Payment")],
            actor_user_id=ACTOR_USER_ID,
            source_report_id=None,
        )
    finally:
        TENANT_CTX.reset(token)

    row = session.scalars(select(AdSensePaymentORM)).one()
    assert row.tenant_id == SECOND_TENANT_ID


def test_sync_payments_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on writes."""
    session = build_session()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        SqlAlchemyAdSensePaymentRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).sync_payments(
            payments=[_payment_input(payment_name="Explicit Wins Payment")],
            actor_user_id=ACTOR_USER_ID,
            source_report_id=None,
        )
    finally:
        TENANT_CTX.reset(token)

    row = session.scalars(select(AdSensePaymentORM)).one()
    assert row.tenant_id == DEFAULT_TENANT_ID


def test_sync_payments_allows_same_payment_name_in_two_tenants() -> None:
    """Per-tenant uniqueness lets two tenants share (month, payment_name)."""
    session = build_session()

    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).sync_payments(
        payments=[
            _payment_input(payment_name="AdSense April Payment", amount="500.00")
        ],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )
    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).sync_payments(
        payments=[
            _payment_input(payment_name="AdSense April Payment", amount="900.00")
        ],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )

    all_rows = session.scalars(select(AdSensePaymentORM)).all()
    assert len(all_rows) == 2
    assert {row.tenant_id for row in all_rows} == {
        DEFAULT_TENANT_ID,
        SECOND_TENANT_ID,
    }

    rows = {row.tenant_id: row.payment_amount for row in all_rows}
    assert set(rows.keys()) == {DEFAULT_TENANT_ID, SECOND_TENANT_ID}
    assert rows[DEFAULT_TENANT_ID] == Decimal("500.00")
    assert rows[SECOND_TENANT_ID] == Decimal("900.00")


def test_sync_payments_upsert_is_scoped_to_one_tenant() -> None:
    """Re-syncing in tenant A must not touch tenant B's row with the same name."""
    session = build_session()

    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).sync_payments(
        payments=[_payment_input(payment_name="Cross Tenant Name", amount="100.00")],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )
    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).sync_payments(
        payments=[_payment_input(payment_name="Cross Tenant Name", amount="200.00")],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )
    SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).sync_payments(
        payments=[_payment_input(payment_name="Cross Tenant Name", amount="111.00")],
        actor_user_id=ACTOR_USER_ID,
        source_report_id=None,
    )

    all_rows = session.scalars(select(AdSensePaymentORM)).all()
    assert len(all_rows) == 2
    assert {row.tenant_id for row in all_rows} == {
        DEFAULT_TENANT_ID,
        SECOND_TENANT_ID,
    }

    rows = {row.tenant_id: row.payment_amount for row in all_rows}
    assert rows[DEFAULT_TENANT_ID] == Decimal("111.00")
    assert rows[SECOND_TENANT_ID] == Decimal("200.00")


# ---------------------------------------------------------------------------
# list_payments / reads
# ---------------------------------------------------------------------------


def test_list_payments_filters_to_explicit_tenant_id() -> None:
    """Explicit tenant injection isolates list_payments reads."""
    session = build_session()
    _seed_payment(
        session, tenant_id=DEFAULT_TENANT_ID, payment_name="Default Tenant Row"
    )
    _seed_payment(
        session, tenant_id=SECOND_TENANT_ID, payment_name="Second Tenant Row"
    )

    page = SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).list_payments(limit=10)

    assert [entry.payment_name for entry in page.items] == ["Second Tenant Row"]
    assert page.has_more is False


def test_list_payments_orders_account_scoped_duplicates_deterministically() -> None:
    """Account-scoped duplicate names paginate in a stable total order."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Shared Payment Name",
        month="2026-04",
        source_account_id="pub-b",
        payment_id=UUID("00000000-0000-0000-0000-0000000000b2"),
    )
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Shared Payment Name",
        month="2026-04",
        source_account_id="pub-a",
        payment_id=UUID("00000000-0000-0000-0000-0000000000a1"),
    )

    page = SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).list_payments(limit=10)

    assert [(entry.source_account_id, entry.id) for entry in page.items] == [
        ("pub-a", "00000000-0000-0000-0000-0000000000a1"),
        ("pub-b", "00000000-0000-0000-0000-0000000000b2"),
    ]


def test_list_payments_uses_default_tenant_without_context() -> None:
    """Bootstrap callers see only UMS-default rows on reads."""
    session = build_session()
    _seed_payment(
        session, tenant_id=DEFAULT_TENANT_ID, payment_name="Default Tenant Row"
    )
    _seed_payment(
        session, tenant_id=SECOND_TENANT_ID, payment_name="Second Tenant Row"
    )

    page = SqlAlchemyAdSensePaymentRepository(session).list_payments(limit=10)

    assert [entry.payment_name for entry in page.items] == ["Default Tenant Row"]


def test_list_payments_returns_empty_page_for_empty_tenant() -> None:
    """Tenant filters return an empty page instead of leaking rows."""
    session = build_session()
    _seed_payment(
        session, tenant_id=DEFAULT_TENANT_ID, payment_name="Default Tenant Row"
    )
    _seed_payment(
        session, tenant_id=SECOND_TENANT_ID, payment_name="Second Tenant Row"
    )

    page = SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=uuid4()
    ).list_payments(limit=10)

    assert page.items == []
    assert page.has_more is False


def test_list_payments_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on reads."""
    session = build_session()
    _seed_payment(
        session, tenant_id=DEFAULT_TENANT_ID, payment_name="Default Tenant Row"
    )
    _seed_payment(
        session, tenant_id=SECOND_TENANT_ID, payment_name="Second Tenant Row"
    )
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        page = SqlAlchemyAdSensePaymentRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).list_payments(limit=10)
    finally:
        TENANT_CTX.reset(token)

    assert [entry.payment_name for entry in page.items] == ["Default Tenant Row"]


def test_list_payments_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes reads when no explicit tenant is supplied."""
    session = build_session()
    _seed_payment(
        session, tenant_id=DEFAULT_TENANT_ID, payment_name="Default Tenant Row"
    )
    _seed_payment(
        session, tenant_id=SECOND_TENANT_ID, payment_name="Second Tenant Row"
    )
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        page = SqlAlchemyAdSensePaymentRepository(session).list_payments(limit=10)
    finally:
        TENANT_CTX.reset(token)

    assert [entry.payment_name for entry in page.items] == ["Second Tenant Row"]


# ---------------------------------------------------------------------------
# list_month_payments / month-scoped reads
# ---------------------------------------------------------------------------


def test_list_month_payments_filters_to_explicit_tenant_id() -> None:
    """Month-scoped reads do not surface rows from other tenants."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Default March",
        month="2026-03",
    )
    _seed_payment(
        session,
        tenant_id=SECOND_TENANT_ID,
        payment_name="Second March",
        month="2026-03",
    )

    entries = SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).list_month_payments(month="2026-03")

    assert [entry.payment_name for entry in entries] == ["Second March"]


def test_list_month_payments_returns_empty_when_target_tenant_has_no_rows() -> None:
    """When only the other tenant has rows for the month, the read is empty."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=SECOND_TENANT_ID,
        payment_name="Second Only",
        month="2026-03",
    )

    entries = SqlAlchemyAdSensePaymentRepository(
        session, tenant_id=DEFAULT_TENANT_ID
    ).list_month_payments(month="2026-03")

    assert entries == []


def test_list_month_payments_uses_default_tenant_without_context() -> None:
    """Bootstrap callers see only UMS-default rows on month-scoped reads."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Default March",
        month="2026-03",
    )
    _seed_payment(
        session,
        tenant_id=SECOND_TENANT_ID,
        payment_name="Second March",
        month="2026-03",
    )

    entries = SqlAlchemyAdSensePaymentRepository(session).list_month_payments(
        month="2026-03"
    )

    assert [entry.payment_name for entry in entries] == ["Default March"]


def test_list_month_payments_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes month reads when no explicit tenant is supplied."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Default March",
        month="2026-03",
    )
    _seed_payment(
        session,
        tenant_id=SECOND_TENANT_ID,
        payment_name="Second March",
        month="2026-03",
    )
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        entries = SqlAlchemyAdSensePaymentRepository(session).list_month_payments(
            month="2026-03"
        )
    finally:
        TENANT_CTX.reset(token)

    assert [entry.payment_name for entry in entries] == ["Second March"]


def test_list_month_payments_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on month reads."""
    session = build_session()
    _seed_payment(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        payment_name="Default March",
        month="2026-03",
    )
    _seed_payment(
        session,
        tenant_id=SECOND_TENANT_ID,
        payment_name="Second March",
        month="2026-03",
    )
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        entries = SqlAlchemyAdSensePaymentRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).list_month_payments(month="2026-03")
    finally:
        TENANT_CTX.reset(token)

    assert [entry.payment_name for entry in entries] == ["Default March"]


# ---------------------------------------------------------------------------
# constructor input validation
# ---------------------------------------------------------------------------


def test_adsense_repository_rejects_invalid_tenant_id_string() -> None:
    """Malformed constructor tenant ids fail closed before any DB access."""
    session = build_session()

    with pytest.raises(
        AdSensePaymentValidationError, match="tenant_id must be a valid UUID"
    ):
        SqlAlchemyAdSensePaymentRepository(session, tenant_id="not-a-uuid")


# ---------------------------------------------------------------------------
# source_account_id keying (account-scoped upsert + dedupe)
# ---------------------------------------------------------------------------


def _payment(**overrides) -> AdSensePaymentInput:
    """Build an account-scoped AdSense payment input for repository writes."""
    base = dict(
        source_account_id="pub-1",
        month="2026-04",
        payment_name="2026-04-21",
        payment_date=date(2026, 4, 21),
        payment_amount=Decimal("930.000000"),
        payment_currency="USD",
        payment_status="PAID",
        raw_payload={"name": "accounts/pub-1/payments/2026-04-21"},
    )
    base.update(overrides)
    return AdSensePaymentInput(**base)


def test_sync_allows_same_month_payment_name_across_accounts() -> None:
    """The same payment suffix can be stored for different AdSense accounts."""
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-1")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-2")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    session.commit()
    rows = session.scalars(
        select(AdSensePaymentORM).where(
            AdSensePaymentORM.tenant_id == DEFAULT_TENANT_ID,
            AdSensePaymentORM.month == "2026-04",
            AdSensePaymentORM.payment_name == "2026-04-21",
        )
    ).all()
    assert {r.source_account_id for r in rows} == {"pub-1", "pub-2"}
    assert len(rows) == 2  # different accounts are NOT a conflict


def test_sync_is_idempotent_per_account_month_name() -> None:
    """The new uniqueness key remains idempotent for one account/month/name."""
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    first = repo.sync_payments(
        payments=[_payment(payment_amount=Decimal("930.000000"))],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    second = repo.sync_payments(
        payments=[_payment(payment_amount=Decimal("931.000000"))],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    session.commit()
    assert first[0].id == second[0].id  # same row updated in place
    assert second[0].source_account_id == "pub-1"


def test_within_batch_duplicate_keys_on_account_month_name() -> None:
    """Reject duplicate account/month/name rows inside a single sync batch."""
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    with pytest.raises(AdSensePaymentValidationError, match="duplicate"):
        repo.sync_payments(
            payments=[_payment(source_account_id="pub-1"),
                      _payment(source_account_id="pub-1")],
            actor_user_id=ACTOR_USER_ID, source_report_id=None,
        )
    # Same month+name but different accounts in one batch is allowed:
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-1"),
                  _payment(source_account_id="pub-2")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )


def test_sync_rejects_blank_source_account_id() -> None:
    """Repository validation rejects blank source account ids fail-closed."""
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    with pytest.raises(AdSensePaymentValidationError, match="source_account_id"):
        repo.sync_payments(
            payments=[_payment(source_account_id="   ")],
            actor_user_id=ACTOR_USER_ID, source_report_id=None,
        )
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # fail closed
