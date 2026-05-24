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


def test_upsert_many_preserves_id_on_conflict(session: Session) -> None:
    """Re-upserting the same source_row_key must preserve the original id.

    Guards against a future refactor that accidentally adds 'id': uuid4()
    to the ON CONFLICT set_ clause.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    key = "d" * 64
    first = repo.upsert_many(
        TENANT_A, [_row(source_row_key=key, amount="100.000000")],
        raw_file_id=RAW_FILE_ID, imported_by=None,
    )
    second = repo.upsert_many(
        TENANT_A, [_row(source_row_key=key, amount="150.000000")],
        raw_file_id=RAW_FILE_ID, imported_by=None,
    )
    assert first[0].id == second[0].id, "id must be preserved on conflict update"


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


def test_rejects_nan_amount_native(session: Session) -> None:
    """A non-finite amount must raise the typed validation error, not leak
    decimal.InvalidOperation from `Decimal('NaN') < 0`.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="u" * 64, amount="NaN")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_decimal_amount(session: Session) -> None:
    """A non-Decimal amount_native must raise the typed validation error, not
    AttributeError from calling .is_finite() on a non-Decimal.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="x" * 64), amount_native="1.23")  # str, not Decimal
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_str_source_system(session: Session) -> None:
    """A non-str source_system must raise the typed validation error, not a raw
    TypeError from `unhashable not in frozenset` membership.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="y" * 64), source_system=["youtube_reporting"])
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_str_source_row_key(session: Session) -> None:
    """A non-str source_row_key must raise the typed validation error, not a raw
    TypeError from len() on a non-sized value.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="z" * 64), source_row_key=12345)
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_str_currency_code(session: Session) -> None:
    """A non-str currency_code must raise the typed validation error in
    _validate, not a raw TypeError when upsert_many builds the currency set
    ({r.currency_code ...}) for the _require_currencies pre-check.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="c" * 64), currency_code=["USD"])  # unhashable
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_str_required_text_column(session: Session) -> None:
    """A required text column written straight to SQL (e.g. report_type) must
    raise the typed validation error if non-str, not surface later as a
    driver/DB error.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="m" * 64), report_type=123)
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_malformed_report_month(session: Session) -> None:
    """A report_month that is not YYYY-MM with month 01-12 must raise the typed
    validation error at the repository boundary, mirroring the DB CHECK rather
    than deferring to a raw IntegrityError on flush.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="n" * 64), report_month="2026-13")
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_reversed_period(session: Session) -> None:
    """period_end before period_start must raise the typed validation error,
    mirroring the DB period-order CHECK instead of a raw IntegrityError.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(
        _row(source_row_key="o" * 64),
        period_start=date(2026, 4, 30),
        period_end=date(2026, 4, 1),
    )
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_report_month_period_mismatch(session: Session) -> None:
    """A well-formed report_month that disagrees with period_start's calendar
    month (e.g. '2026-05' over an April period) must raise the typed validation
    error, so a non-parser caller cannot misattribute revenue to the wrong
    month bucket.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="p" * 64), report_month="2026-05")  # period is April
    with pytest.raises(GoogleRevenueSourceRowValidationError):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_validate_deep_copies_nested_raw_payload(session: Session) -> None:
    """_validate must deep-copy raw_payload so a caller mutating a NESTED dict
    cannot reach into the persisted row. A shallow dict() copy shares the
    nested objects (date_range/dimensions/metrics) with the caller.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    nested: dict[str, object] = {"dimensions": {"channel": "UC_x", "country": "US"}}
    validated = repo._validate(replace(_row(source_row_key="v" * 64), raw_payload=nested))
    # Mutate the caller's nested dict; the validated copy must be unaffected.
    nested["dimensions"]["country"] = "MUTATED"
    assert validated.raw_payload == {"dimensions": {"channel": "UC_x", "country": "US"}}


def test_read_path_deep_copies_nested_raw_payload(session: Session) -> None:
    """The read path (_to_entry) must deep-copy raw_payload too: mutating a
    nested dict on a returned entry must not alias the live ORM row and leak
    into a subsequent read.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    key = "rd" * 32  # 64 chars
    repo.upsert_many(
        TENANT_A,
        [replace(_row(source_row_key=key), raw_payload={"dimensions": {"country": "US"}})],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    first = repo.get_exact(TENANT_A, source_system="youtube_reporting", source_row_key=key)
    assert first is not None
    first.raw_payload["dimensions"]["country"] = "MUTATED"  # caller mutates a nested object
    second = repo.get_exact(TENANT_A, source_system="youtube_reporting", source_row_key=key)
    assert second is not None
    assert second.raw_payload == {"dimensions": {"country": "US"}}


def test_non_usd_source_rows_visible_at_repository_layer(session: Session) -> None:
    """B1 makes non-USD source rows queryable at the repository layer.

    Any future surfacing through an API endpoint is out of scope.
    This test pins the visibility contract: a caller that asks for all
    source rows for a tenant/month MUST see non-USD rows alongside USD
    rows - no silent filter, no automatic conversion.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    base = _row(source_row_key="s" * 64)
    repo.upsert_many(
        TENANT_A,
        [
            base,
            replace(base, source_row_key="t" * 64, currency_code="EGP"),
        ],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )
    rows = repo.list(TENANT_A, report_month="2026-04")
    currencies = {r.currency_code for r in rows}
    assert currencies == {"USD", "EGP"}
