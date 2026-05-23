"""SqlAlchemyGoogleRevenueSourceRowRepository tests.

Asserts:
 - idempotent upsert on (tenant_id, source_system, source_row_key);
 - tenant isolation;
 - mutable-field updates on conflict;
 - domain-level validation of source_system / source_row_key length /
   value_kind / amount / raw_payload type / currency_code existence;
 - tenant-scoped list + get_exact lookup.

The SQLite engine is used for speed; the dialect-aware upsert helper
(_dialect_insert) lets the same code run on PostgreSQL in production.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows import (
    GoogleRevenueSourceRowValidationError,
    ParsedSourceRow,
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

TENANT_A = uuid4()
TENANT_B = uuid4()
RAW_FILE_ID = uuid4()


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            s.add_all(
                [
                    TenantORM(id=TENANT_A, slug="tenant-a", display_name="Tenant A"),
                    TenantORM(id=TENANT_B, slug="tenant-b", display_name="Tenant B"),
                    CurrencyORM(
                        code="USD",
                        numeric_code="840",
                        name="US Dollar",
                        minor_unit=2,
                        is_supported=True,
                        activated_at=datetime.now(UTC),
                    ),
                    CurrencyORM(
                        code="EGP",
                        numeric_code="818",
                        name="Egyptian Pound",
                        minor_unit=2,
                        is_supported=True,
                        activated_at=datetime.now(UTC),
                    ),
                ]
            )
            s.flush()
            yield s
    finally:
        engine.dispose()


def _row(
    *,
    source_row_key: str,
    amount: str = "1234.560000",
    currency: str = "USD",
    source_system: str = "youtube_reporting",
) -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id="acct-001",
        content_owner_id=None,
        youtube_channel_id="UC_test_channel",
        report_type="channel_monthly_estimated_revenue",
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key="estimatedRevenue",
        value_kind="estimated",
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="report-001",
        raw_payload={"sample": "payload"},
    )


def test_upsert_many_inserts_new_rows(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    rows = [_row(source_row_key="a" * 64), _row(source_row_key="b" * 64)]
    written = repo.upsert_many(
        TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None
    )
    assert len(written) == 2

    reloaded = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_A
        )
    ).all()
    assert len(reloaded) == 2


def test_upsert_many_is_idempotent_on_rerun(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    rows = [_row(source_row_key="c" * 64)]
    repo.upsert_many(TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None)
    repo.upsert_many(TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None)
    count = (
        session.query(GoogleRevenueSourceRowORM)
        .filter_by(tenant_id=TENANT_A)
        .count()
    )
    assert count == 1


def test_upsert_many_updates_mutable_fields_on_conflict(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    key = "d" * 64
    repo.upsert_many(
        TENANT_A,
        [_row(source_row_key=key, amount="100.000000")],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    repo.upsert_many(
        TENANT_A,
        [_row(source_row_key=key, amount="150.000000")],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    reloaded = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.source_row_key == key
        )
    ).one()
    assert reloaded.amount_native == Decimal("150.000000")


def test_tenant_isolation(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    shared_key = "e" * 64
    repo.upsert_many(
        TENANT_A,
        [_row(source_row_key=shared_key)],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    repo.upsert_many(
        TENANT_B,
        [_row(source_row_key=shared_key)],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    a_rows = repo.list(TENANT_A, report_month="2026-04")
    b_rows = repo.list(TENANT_B, report_month="2026-04")
    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert a_rows[0].tenant_id != b_rows[0].tenant_id


def test_rejects_invalid_source_system(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="f" * 64, source_system="not_a_real_source")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_short_source_row_key(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="too-short")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_negative_amount(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="g" * 64, amount="-1.000000")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_unknown_currency(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="h" * 64, currency="ZZZ")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_list_by_tenant_and_month(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        TENANT_A,
        [
            _row(source_row_key="i" * 64),
            _row(source_row_key="j" * 64),
        ],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    rows = repo.list(TENANT_A, report_month="2026-04")
    assert len(rows) == 2


def test_get_exact_returns_match(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    key = "k" * 64
    repo.upsert_many(
        TENANT_A,
        [_row(source_row_key=key)],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    entry = repo.get_exact(
        TENANT_A, source_system="youtube_reporting", source_row_key=key
    )
    assert entry is not None
    assert entry.source_row_key == key


def test_get_exact_returns_none_for_missing(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    entry = repo.get_exact(
        TENANT_A, source_system="youtube_reporting", source_row_key="m" * 64
    )
    assert entry is None


def test_list_filters_combine(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        TENANT_A,
        [
            _row(source_row_key="n" * 64, source_system="youtube_reporting"),
            _row(source_row_key="o" * 64, source_system="adsense_management"),
        ],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    filtered = repo.list(
        TENANT_A,
        report_month="2026-04",
        source_system="youtube_reporting",
    )
    assert len(filtered) == 1
    assert filtered[0].source_system == "youtube_reporting"


def test_list_for_channel_returns_only_matches(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

    def _channel_row(key: str, channel: str) -> ParsedSourceRow:
        # dataclasses.replace produces a new frozen ParsedSourceRow with
        # only the youtube_channel_id overridden — clearer than rebuilding
        # every field by hand for each variant.
        return replace(_row(source_row_key=key), youtube_channel_id=channel)

    repo.upsert_many(
        TENANT_A,
        [
            _channel_row("p" * 64, "UC_alpha"),
            _channel_row("q" * 64, "UC_alpha"),
            _channel_row("r" * 64, "UC_beta"),
        ],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    alpha = repo.list_for_channel(
        TENANT_A, youtube_channel_id="UC_alpha", report_month="2026-04"
    )
    assert len(alpha) == 2
    assert {r.youtube_channel_id for r in alpha} == {"UC_alpha"}


def test_upsert_many_does_not_alias_caller_raw_payload(session: Session) -> None:
    """Mutating the caller's raw_payload dict after upsert must not affect persisted state.

    Guards against the repository passing raw_payload by reference into the
    SQLAlchemy insert/update statements without a defensive copy.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    payload: dict[str, object] = {"original_metric": "estimatedRevenue"}
    base = _row(source_row_key="z" * 64)
    row_with_payload = replace(base, raw_payload=payload)
    repo.upsert_many(
        TENANT_A, [row_with_payload],
        raw_file_id=RAW_FILE_ID, imported_by=None,
    )
    # Caller mutates their dict AFTER the call.
    payload["original_metric"] = "MUTATED"
    payload["new_field"] = "leak"

    persisted = repo.get_exact(
        TENANT_A, source_system="youtube_reporting", source_row_key="z" * 64,
    )
    assert persisted is not None
    assert persisted.raw_payload == {"original_metric": "estimatedRevenue"}
    assert "new_field" not in persisted.raw_payload
