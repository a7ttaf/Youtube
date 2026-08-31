# ============================================================================
# Purpose: Verify withholding-rate validation, append-only revisions, and
#   deterministic effective-date reads without wiring an API/estimate surface.
# Database/ORM: SQLite SecurityBase; users and us_withholding_rate_configs.
# Standards: Exact Decimal boundaries, explicit tenants, and latest-revision proof.
# Blast Radius: Test-only finance estimate configuration coverage.
# Connections:
#   - File: backend/ums_smart_revenue/finance/us_withholding_config.py -> Subject.
#   - File: tests/db/test_external_identity_withholding_migration_postgres.py -> Race/RLS proof.
# ============================================================================
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import SecurityBase, UserORM, UsWithholdingRateConfigORM
from ums_smart_revenue.finance.us_withholding_config import (
    InvalidUsWithholdingConfigError,
    SqlAlchemyUsWithholdingConfigRepository,
    UsWithholdingConfigConflictError,
    UsWithholdingConfigStorageError,
    validate_us_withholding_rate,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT_ID = UUID(UMS_TENANT_ID)
USER_ID = UUID("00000000-0000-0000-0000-000000088002")
ACCOUNT_A = "pub-pr228-a"
ACCOUNT_B = "pub-pr228-b"


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'wh.db').as_posix()}")
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            UserORM(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="finance@example.com",
                display_name="Finance",
            )
        )
        db.commit()
        yield db


@pytest.mark.parametrize(
    ("rate", "message"),
    [
        (Decimal("-0.000001"), "between 0 and"),
        (Decimal("0.300001"), "between 0 and"),
        (Decimal("0.1234567"), "at most 6 decimal places"),
        (Decimal("NaN"), "finite"),
        (Decimal("Infinity"), "finite"),
        (Decimal("1E+999999"), "between 0 and"),
    ],
)
def test_validate_us_withholding_rate_rejects_invalid_numeric_values(
    rate: Decimal,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_us_withholding_rate(rate)


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("0.123456"), Decimal("0.30")])
def test_validate_us_withholding_rate_accepts_numeric_boundaries(rate: Decimal) -> None:
    validate_us_withholding_rate(rate)


def test_validate_us_withholding_rate_is_independent_of_ambient_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 5
        validate_us_withholding_rate(Decimal("0.123456"))


def test_get_effective_rate_returns_none_without_config(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    assert (
        repo.get_effective_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_A,
            as_of=date(2026, 4, 30),
        )
        is None
    )


def test_get_effective_rate_picks_latest_effective_from(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 1, 1),
        rate=Decimal("0.15"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.20"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()
    snapshot = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 4, 15),
    )
    assert snapshot is not None
    assert snapshot.rate == Decimal("0.20")
    older = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 2, 1),
    )
    assert older is not None
    assert older.rate == Decimal("0.15")


def test_get_effective_rate_uses_revision_when_timestamp_and_uuid_order_conflict(
    session: Session,
) -> None:
    tied_at = datetime(2026, 4, 1, 12, tzinfo=UTC)
    older_high_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    later_low_id = UUID("00000000-0000-0000-0000-000000000001")
    session.add_all(
        [
            UsWithholdingRateConfigORM(
                id=older_high_id,
                tenant_id=TENANT_ID,
                source_account_id=ACCOUNT_A,
                effective_from=date(2026, 4, 1),
                revision=1,
                rate=Decimal("0.10"),
                account_type="individual",
                confirmed_by_user_id=USER_ID,
                created_at=tied_at,
            ),
            UsWithholdingRateConfigORM(
                id=later_low_id,
                tenant_id=TENANT_ID,
                source_account_id=ACCOUNT_A,
                effective_from=date(2026, 4, 1),
                revision=2,
                rate=Decimal("0.20"),
                account_type="business",
                confirmed_by_user_id=USER_ID,
                created_at=tied_at,
            ),
        ]
    )
    session.commit()

    snapshot = SqlAlchemyUsWithholdingConfigRepository(session).get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 4, 15),
    )

    assert snapshot is not None
    assert snapshot.rate == Decimal("0.20")
    assert snapshot.account_type == "business"
    assert snapshot.revision == 2


def test_record_confirmed_rate_allocates_append_only_same_date_revisions(
    session: Session,
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)

    first = repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.10"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    second = repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.20"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()

    latest = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 4, 1),
    )
    assert first.revision == 1
    assert second.revision == 2
    assert latest is not None
    assert latest.revision == 2
    assert latest.rate == Decimal("0.20")


def test_effective_rate_is_isolated_by_source_account(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    for source_account_id, rate in (
        (ACCOUNT_A, Decimal("0.15")),
        (ACCOUNT_B, Decimal("0.30")),
    ):
        repo.record_confirmed_rate(
            tenant_id=TENANT_ID,
            source_account_id=source_account_id,
            effective_from=date(2026, 4, 1),
            rate=rate,
            account_type="business",
            confirmed_by_user_id=USER_ID,
        )
    session.commit()

    account_a = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 4, 30),
    )
    account_b = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_B,
        as_of=date(2026, 4, 30),
    )
    assert account_a is not None
    assert account_a.rate == Decimal("0.15")
    assert account_a.revision == 1
    assert account_b is not None
    assert account_b.rate == Decimal("0.30")
    assert account_b.revision == 1


def test_missing_exact_source_account_has_no_tenant_wide_fallback(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.15"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()

    assert (
        repo.get_effective_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_B,
            as_of=date(2026, 4, 30),
        )
        is None
    )


@pytest.mark.parametrize(
    "source_account_id",
    [
        "",
        "   ",
        "\t",
        "\n",
        "\r",
        "\f",
        "\v",
        " pub-pr228-a",
        "pub-pr228-a ",
        "pub/pr228-a",
        "pub?pr228-a",
        "pub#pr228-a",
        "pub%pr228-a",
        "accounts/ pub-pr228-a",
        "accounts/\tpub-pr228-a",
    ],
)
def test_source_account_id_must_be_nonblank_and_canonical(
    session: Session,
    source_account_id: str,
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    with pytest.raises(InvalidUsWithholdingConfigError, match="source_account_id"):
        repo.get_effective_rate(
            tenant_id=TENANT_ID,
            source_account_id=source_account_id,
            as_of=date(2026, 4, 30),
        )


def test_account_resource_alias_uses_one_canonical_revision_history(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    first = repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=f"accounts/{ACCOUNT_A}",
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.15"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    second = repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.20"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()

    latest = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=f"accounts/{ACCOUNT_A}",
        as_of=date(2026, 4, 30),
    )
    assert first.source_account_id == ACCOUNT_A
    assert first.revision == 1
    assert second.source_account_id == ACCOUNT_A
    assert second.revision == 2
    assert latest is not None
    assert latest.source_account_id == ACCOUNT_A
    assert latest.revision == 2


def test_record_confirmed_rate_raises_typed_validation_error(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    with pytest.raises(InvalidUsWithholdingConfigError, match="account_type"):
        repo.record_confirmed_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_A,
            effective_from=date(2026, 4, 1),
            rate=Decimal("0.15"),
            account_type="unknown",
            confirmed_by_user_id=USER_ID,
        )


def test_record_confirmed_rate_translates_unique_conflict(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 1),
        rate=Decimal("0.15"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    session.commit()
    monkeypatch.setattr(repo, "_next_revision", lambda **_kwargs: 1)

    with pytest.raises(UsWithholdingConfigConflictError, match="conflicts with stored state"):
        repo.record_confirmed_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_A,
            effective_from=date(2026, 4, 1),
            rate=Decimal("0.20"),
            account_type="business",
            confirmed_by_user_id=USER_ID,
        )
    session.rollback()


def test_get_effective_rate_translates_storage_error(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)

    def raise_storage_error(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(session, "scalar", raise_storage_error)
    with pytest.raises(UsWithholdingConfigStorageError, match="Unable to load"):
        repo.get_effective_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_A,
            as_of=date(2026, 4, 30),
        )


def test_no_default_rate_row_seeded(session: Session) -> None:
    count = session.query(UsWithholdingRateConfigORM).count()
    assert count == 0


# ============================================================================
# Purpose: Prove the review-P2 isolation contract and the review-P3 quantized
#   representation of accepted trailing-zero rates.
# Connections:
#   - File: backend/ums_smart_revenue/finance/us_withholding_config.py -> Guards.
# ============================================================================
def test_next_revision_refuses_higher_isolation_levels(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    postgres_bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(session, "get_bind", lambda: postgres_bind)
    monkeypatch.setattr(session, "execute", lambda *_a, **_k: None)
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: "repeatable read")
    with pytest.raises(UsWithholdingConfigStorageError, match="READ COMMITTED"):
        repo.record_confirmed_rate(
            tenant_id=TENANT_ID,
            source_account_id=ACCOUNT_A,
            effective_from=date(2026, 4, 30),
            rate=Decimal("0.15"),
            account_type="business",
            confirmed_by_user_id=USER_ID,
        )


def test_next_revision_allows_read_committed_contract(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    postgres_bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    scalar_results = iter(["read committed", 3])
    monkeypatch.setattr(session, "get_bind", lambda: postgres_bind)
    monkeypatch.setattr(session, "execute", lambda *_a, **_k: None)
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: next(scalar_results))
    revision = repo._next_revision(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 30),
    )
    assert revision == 4


def test_recorded_rate_quantizes_trailing_zero_excess(session: Session) -> None:
    repo = SqlAlchemyUsWithholdingConfigRepository(session)
    snapshot = repo.record_confirmed_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        effective_from=date(2026, 4, 30),
        rate=Decimal("0.1500000"),
        account_type="business",
        confirmed_by_user_id=USER_ID,
    )
    assert snapshot.rate == Decimal("0.150000")
    assert str(snapshot.rate) == "0.150000"
    session.expire_all()
    reloaded = repo.get_effective_rate(
        tenant_id=TENANT_ID,
        source_account_id=ACCOUNT_A,
        as_of=date(2026, 5, 31),
    )
    assert reloaded is not None
    assert str(reloaded.rate) == str(snapshot.rate)
