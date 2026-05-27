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
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

TENANT_A = uuid4()
TENANT_B = uuid4()
RAW_FILE_ID = uuid4()  # owned by TENANT_A
RAW_FILE_ID_B = uuid4()  # owned by TENANT_B


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            s.add_all(
                [
                    TenantORM(id=TENANT_A, slug="tenant-a", display_name="Tenant A"),
                    TenantORM(id=TENANT_B, slug="tenant-b", display_name="Tenant B"),
                    # Raw evidence files are tenant-scoped; seed one per tenant
                    # so upsert_many's tenant-scoped raw_file_id pre-check passes
                    # for same-tenant links and rejects cross-tenant ones.
                    RawReportFileORM(
                        id=RAW_FILE_ID,
                        tenant_id=TENANT_A,
                        source="youtube_reporting",
                        report_type="channel_monthly_estimated_revenue",
                        report_month="2026-04",
                        file_url="memory://tenant-a/raw.json",
                        checksum="a" * 64,
                    ),
                    RawReportFileORM(
                        id=RAW_FILE_ID_B,
                        tenant_id=TENANT_B,
                        source="youtube_reporting",
                        report_type="channel_monthly_estimated_revenue",
                        report_month="2026-04",
                        file_url="memory://tenant-b/raw.json",
                        checksum="b" * 64,
                    ),
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
    source_account_id: str = "acct-001",
    report_type: str = "channel_monthly_estimated_revenue",
) -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id=source_account_id,
        content_owner_id=None,
        youtube_channel_id="UC_test_channel",
        report_type=report_type,
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
        raw_file_id=RAW_FILE_ID_B,
        imported_by=None,
    )
    a_rows = repo.list(TENANT_A, report_month="2026-04")
    b_rows = repo.list(TENANT_B, report_month="2026-04")
    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert a_rows[0].tenant_id != b_rows[0].tenant_id


def test_rejects_unknown_tenant(session: Session) -> None:
    # An unseeded tenant_id must fail with the typed error, not a raw FK error.
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    ok = _row(source_row_key="t" * 64)
    with pytest.raises(GoogleRevenueSourceRowValidationError, match="unknown tenant_id"):
        repo.upsert_many(uuid4(), [ok], raw_file_id=None, imported_by=None)


def test_rejects_unknown_raw_file(session: Session) -> None:
    # A raw_file_id with no backing row must fail with the typed error.
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    ok = _row(source_row_key="u" * 64)
    with pytest.raises(GoogleRevenueSourceRowValidationError, match="not found for this tenant"):
        repo.upsert_many(TENANT_A, [ok], raw_file_id=uuid4(), imported_by=None)


def test_rejects_cross_tenant_raw_file(session: Session) -> None:
    # TENANT_A rows must not link to TENANT_B's raw evidence file: a row can
    # never be attributed to another tenant's provenance (tenant isolation).
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    ok = _row(source_row_key="v" * 64)
    with pytest.raises(GoogleRevenueSourceRowValidationError, match="not found for this tenant"):
        repo.upsert_many(TENANT_A, [ok], raw_file_id=RAW_FILE_ID_B, imported_by=None)


def test_allows_none_raw_file(session: Session) -> None:
    # Provenance is optional: a None raw_file_id skips the FK pre-check.
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    ok = _row(source_row_key="w" * 64)
    written = repo.upsert_many(TENANT_A, [ok], raw_file_id=None, imported_by=None)
    assert len(written) == 1


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


def test_rejects_unicode_digit_report_month(session: Session) -> None:
    """report_month with Unicode decimal digits (e.g. Arabic-Indic '٢٠٢٦-٠٤')
    must raise the typed validation error. The Python guard uses [0-9] (ASCII)
    to mirror the ASCII-only DB CHECK, so such a value cannot slip past
    _validate and surface as a raw IntegrityError on flush.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="ud" * 32), report_month="٢٠٢٦-٠٤")
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"report_month must be YYYY-MM"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_str_nullable_text_column(session: Session) -> None:
    """A nullable text column (content_owner_id) accepts None, but a non-str,
    non-None value (list/dict from a loose non-parser caller) must raise the
    typed validation error rather than flowing into SQL as a driver/DB error.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="nl" * 32), content_owner_id=["cms-1"])
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"content_owner_id must be a str or None"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_datetime_period_bounds(session: Session) -> None:
    """datetime is a subclass of date; a timestamp must raise the typed
    validation error instead of being silently truncated into the DATE columns,
    which would alter the caller-provided period boundaries.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(
        _row(source_row_key="dt" * 32),
        period_start=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        period_end=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"must be date values"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_amount_exceeding_six_fractional_digits(session: Session) -> None:
    """amount_native maps to Numeric(20, 6); a value with >6 fractional digits
    would be silently ROUNDED by the DB on write, altering the source amount.
    It must raise the typed validation error at the boundary instead.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="sc" * 32, amount="1.1234567")  # 7 fractional digits
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"6 fractional digits"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_accepts_amount_at_six_fractional_digits(session: Session) -> None:
    """Exactly 6 fractional digits is the Numeric(20, 6) scale and must be
    accepted unchanged — the boundary guard rejects >6, not ==6.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    ok = _row(source_row_key="s6" * 32, amount="1.123456")
    written = repo.upsert_many(TENANT_A, [ok], raw_file_id=RAW_FILE_ID, imported_by=None)
    assert written[0].amount_native == Decimal("1.123456")


def test_rejects_non_json_serialisable_raw_payload(session: Session) -> None:
    """raw_payload is written to a JSON/JSONB column; a dict holding a
    non-serialisable value (a set) must raise the typed validation error rather
    than failing late as an opaque driver serialization error on flush.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="js" * 32), raw_payload={"tags": {1, 2, 3}})
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"raw_payload must be JSON-serialisable"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_upsert_preserves_provenance_when_reimport_omits_it(session: Session) -> None:
    """Re-importing an existing row through a path that lacks provenance
    (raw_file_id/imported_by = None) must NOT erase the audit lineage recorded
    on the original ingest. A genuinely new file/importer still replaces it.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    key = "pv" * 32
    importer_1 = uuid4()
    file_2 = uuid4()
    importer_2 = uuid4()

    # Phase 1: first import carries provenance.
    repo.upsert_many(
        TENANT_A, [_row(source_row_key=key)],
        raw_file_id=RAW_FILE_ID, imported_by=importer_1,
    )
    # Phase 2: replay without provenance (both None) must preserve the original.
    repo.upsert_many(
        TENANT_A, [_row(source_row_key=key, amount="999.000000")],
        raw_file_id=None, imported_by=None,
    )
    # expire_all so the verification reads fresh DB state rather than the
    # session's identity-map copy (which the upsert does not re-populate).
    session.expire_all()
    preserved = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.source_row_key == key
        )
    ).one()
    assert preserved.amount_native == Decimal("999.000000")  # mutable field still updated
    assert preserved.raw_file_id == RAW_FILE_ID  # provenance preserved, not nulled
    assert preserved.imported_by == importer_1

    # Phase 3: a genuinely new file/importer (non-None) does replace. file_2 is
    # a real TENANT_A-owned raw file so it passes the tenant-scoped FK pre-check.
    session.add(
        RawReportFileORM(
            id=file_2,
            tenant_id=TENANT_A,
            source="youtube_reporting",
            report_type="channel_monthly_estimated_revenue",
            report_month="2026-04",
            file_url="memory://tenant-a/raw-2.json",
            checksum="d" * 64,
        )
    )
    session.flush()
    repo.upsert_many(
        TENANT_A, [_row(source_row_key=key)],
        raw_file_id=file_2, imported_by=importer_2,
    )
    session.expire_all()
    replaced = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.source_row_key == key
        )
    ).one()
    assert replaced.raw_file_id == file_2
    assert replaced.imported_by == importer_2


def test_delete_stale_for_scope_preserves_other_scopes(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    keep_key = "dk" * 32
    stale_key = "ds" * 32
    other_account_key = "da" * 32
    other_type_key = "dt" * 32
    repo.upsert_many(
        TENANT_A,
        [
            _row(source_row_key=keep_key, source_account_id="acct-a"),
            _row(source_row_key=stale_key, source_account_id="acct-a"),
            _row(source_row_key=other_account_key, source_account_id="acct-b"),
            _row(
                source_row_key=other_type_key,
                source_account_id="acct-a",
                report_type="other_report_type",
            ),
        ],
        raw_file_id=RAW_FILE_ID,
        imported_by=None,
    )

    deleted = repo.delete_stale_for_scope(
        TENANT_A,
        source_system="youtube_reporting",
        source_account_id="acct-a",
        report_type="channel_monthly_estimated_revenue",
        report_month="2026-04",
        keep_source_row_keys=[keep_key],
    )

    assert deleted == 1
    remaining = {
        row.source_row_key
        for row in session.scalars(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == TENANT_A
            )
        )
    }
    assert stale_key not in remaining
    assert {keep_key, other_account_key, other_type_key}.issubset(remaining)


def test_rejects_amount_exceeding_integer_precision(session: Session) -> None:
    """Numeric(20, 6) allows at most 14 integer digits; a larger integer part
    must raise the typed validation error rather than failing later as a raw
    PostgreSQL numeric field overflow on INSERT.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = _row(source_row_key="ip" * 32, amount="100000000000000")  # 10**14 → 15 integer digits
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"14 integer digits"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_validate_accepts_amount_at_max_numeric_precision(session: Session) -> None:
    """The full Numeric(20, 6) envelope — 14 integer + 6 fractional digits — must
    pass validation unchanged; the precision/scale guards reject only values
    beyond it. Asserted via _validate directly, not an upsert round-trip:
    SQLite stores Numeric as float64, which cannot hold 20 significant digits
    (real Numeric(20,6) storage is covered by the PostgreSQL round-trip test).
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    row = _row(source_row_key="i4" * 32, amount="99999999999999.999999")
    validated = repo._validate(row)
    assert validated.amount_native == Decimal("99999999999999.999999")


def test_validate_accepts_zero_amount_with_positive_exponent(session: Session) -> None:
    """A zero is valid revenue (0 integer digits) and must pass the integer-width
    guard regardless of Decimal representation. Decimal('0E+20') — which a parser
    can emit — has adjusted()==20 and previously tripped the >=14 integer-digit
    guard even though it is exactly 0 and fits Numeric(20, 6). Asserted via
    _validate directly (SQLite cannot round-trip the exotic exponent faithfully).
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    validated = repo._validate(_row(source_row_key="z0" * 32, amount="0E+20"))
    assert validated.amount_native == Decimal(0)


def test_rejects_non_str_raw_payload_keys(session: Session) -> None:
    """json.dumps silently coerces non-string dict keys (int 1 → "1"), which
    would mutate audit evidence and can collide keys. The repository must reject
    non-string keys (including nested) to keep the dict[str, object] contract.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad_top = replace(_row(source_row_key="ky" * 32), raw_payload={1: "numeric-key"})
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"keys must be strings"):
        repo.upsert_many(TENANT_A, [bad_top], raw_file_id=RAW_FILE_ID, imported_by=None)
    bad_nested = replace(_row(source_row_key="kn" * 32), raw_payload={"dimensions": {2: "v"}})
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"keys must be strings"):
        repo.upsert_many(TENANT_A, [bad_nested], raw_file_id=RAW_FILE_ID, imported_by=None)


def test_rejects_non_finite_float_in_raw_payload(session: Session) -> None:
    """raw_payload with NaN/Infinity floats must be rejected — PostgreSQL JSONB
    disallows them, so json.dumps(allow_nan=False) surfaces it as the typed
    validation error instead of a late driver error on flush.
    """
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    bad = replace(_row(source_row_key="nf" * 32), raw_payload={"v": float("inf")})
    with pytest.raises(GoogleRevenueSourceRowValidationError, match=r"finite numbers"):
        repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


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
