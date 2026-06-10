"""PostgreSQL round-trip for 20260531_0001 (channel-account map tables)."""
from pathlib import Path

import pytest
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_url() -> str:
    """Return the PostgreSQL database URL for testing."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Create an Alembic Config pointed at the test Postgres URL and script location."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    """Provide a fresh engine with a clean public schema for each test."""
    # ============================================================================
    # Purpose: Provide a fresh SQLAlchemy engine with a clean `public` schema
    # for one migration round-trip test. Bounded by
    # `SET LOCAL lock_timeout = '30s'` so a contended reset (e.g. an orphan
    # `idle in transaction` connection from a prior killed/hung run on a
    # shared/reused cluster) fails fast with a diagnosable `LockNotAvailable`
    # error instead of hanging indefinitely.
    # Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`.
    # Standards: `SET LOCAL` is transaction-scoped (reverts on
    # `engine.begin()` commit), so the existing lock-blocking tests that set
    # their own `statement_timeout='750ms'` on contender connections are
    # unaffected. The `try/finally` wrapper guarantees `engine.dispose()`
    # runs even when the schema reset raises — no leaked pool on the
    # contended-reset failure path.
    # Blast Radius: None detected — test-harness fixture only; no product
    # code, no migration, no `alembic/env.py` change.
    # Connections:
    #   - File: tests/_postgres_helpers.py -> `require_postgres_url()`
    #     supplies the disposable test DB URL.
    #   - File: AGENTS.md -> "Professional Commenting Standard" (this block).
    #   - File: tests/db/test_tenant_rls_migration.py -> `_drop_public_schema`
    #     is the reference try/finally engine-disposal pattern.
    # ============================================================================
    engine = create_engine(postgres_url)
    try:
        with engine.begin() as conn:
            # Fail fast (don't hang) if a stray connection holds a
            # public-schema lock: without lock_timeout, DROP SCHEMA waits
            # indefinitely.
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        yield engine
    finally:
        engine.dispose()


def test_upgrade_creates_both_tables_with_constraints(alembic_config, fresh_engine):
    """Upgrade creates both map tables with expected constraints, unique keys, and indexes."""
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" in inspector.get_table_names()
    assert "content_owner_channel_links" in inspector.get_table_names()
    checks = {
        c["name"] for c in inspector.get_check_constraints("adsense_content_owner_links")
    }
    assert "ck_adsense_content_owner_links_status" in checks
    assert "ck_adsense_content_owner_links_range" in checks
    assert "ck_adsense_content_owner_links_provenance_payload_object" in checks
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("adsense_content_owner_links")
    }
    assert uniques["uq_adsense_content_owner_links_key"] == (
        "tenant_id", "adsense_account_id", "content_owner_id", "effective_month_start",
    )
    fks = {c["name"] for c in inspector.get_foreign_keys("content_owner_channel_links")}
    assert "fk_content_owner_channel_links_tenant" in fks
    uniques2 = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("content_owner_channel_links")
    }
    assert uniques2["uq_content_owner_channel_links_key"] == (
        "tenant_id", "content_owner_id", "youtube_channel_id", "effective_month_start",
    )
    indexes = {c["name"] for c in inspector.get_indexes("adsense_content_owner_links")} | {
        c["name"] for c in inspector.get_indexes("content_owner_channel_links")
    }
    assert "ix_adsense_content_owner_links_account_status" in indexes
    assert "ix_content_owner_channel_links_owner" in indexes


def test_downgrade_drops_both_tables(alembic_config, fresh_engine):
    """Downgrade removes both map tables."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" not in inspector.get_table_names()
    assert "content_owner_channel_links" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    """Upgrade -> downgrade -> upgrade completes without errors."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    command.upgrade(alembic_config, "head")
    assert "adsense_content_owner_links" in inspect(fresh_engine).get_table_names()


def test_provenance_payload_object_check_rejects_array(alembic_config, fresh_engine):
    """provenance_payload object CHECK rejects a JSON array."""
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO adsense_content_owner_links "
        "(tenant_id, adsense_account_id, content_owner_id, provenance_kind, "
        "provenance_payload, effective_month_start) VALUES "
        "(:tenant, 'pub-1', 'owner-1', 'OPERATOR_ASSERTED', '[]'::jsonb, '2026-01')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})


def test_owner_channel_provenance_kind_check_rejects_unknown(alembic_config, fresh_engine):
    """provenance_kind CHECK rejects unknown values on content_owner_channel_links."""
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO content_owner_channel_links "
        "(tenant_id, content_owner_id, youtube_channel_id, provenance_kind, "
        "effective_month_start) VALUES "
        "(:tenant, 'owner-1', 'chan-1', 'BOGUS', '2026-04')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})


def test_repo_verify_blocks_on_held_per_account_advisory_lock(alembic_config, fresh_engine):
    """Proof the repository verify path acquires the per-account advisory lock.

    Connection A holds pg_advisory_xact_lock for (tenant, "pub-lock"). A second
    Session runs the repository's verify_account_owner_link under a short
    statement_timeout; because verify acquires the SAME per-account lock, the
    lock wait is canceled and raises. Commenting out _acquire_account_owner_lock
    in verify makes this test fail (the contender would not block).
    """
    from uuid import UUID, uuid4

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
        _account_owner_lock_key,
    )

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)

    # Seed one UNVERIFIED link (committed so the contender Session can load it).
    with Session(fresh_engine) as setup:
        link_id = (
            SqlAlchemyChannelAccountLinkRepository(setup, tenant_id=tenant)
            .propose_account_owner_link(
                adsense_account_id="pub-lock", content_owner_id="owner-1",
                effective_month_start="2026-01", effective_month_end=None,
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )
            .id
        )
        setup.commit()

    key = _account_owner_lock_key(tenant, "pub-lock")
    holder = fresh_engine.connect()
    holder_txn = holder.begin()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        with Session(fresh_engine) as contender:
            # statement_timeout reliably cancels a blocked advisory-lock wait.
            contender.execute(text("SET statement_timeout = '750ms'"))
            repo = SqlAlchemyChannelAccountLinkRepository(contender, tenant_id=tenant)
            with pytest.raises(OperationalError) as exc_info:
                repo.verify_account_owner_link(link_id, verified_by=uuid4(), reason="x")
            err_msg = str(exc_info.value).lower()
            assert "canceling statement" in err_msg or "statement timeout" in err_msg, (
                "verify must be canceled by lock-wait timeout, not another OperationalError"
            )
    finally:
        holder_txn.rollback()
        holder.close()


def test_repo_verify_succeeds_uncontended_on_postgres(alembic_config, fresh_engine):
    """Happy path: the verify path runs end-to-end against live Postgres."""
    from uuid import UUID, uuid4

    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)
    with Session(fresh_engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=tenant)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-pg", content_owner_id="owner-pg",
            effective_month_start="2026-01", effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
        )
        out = repo.verify_account_owner_link(link.id, verified_by=uuid4(), reason="ok")
        session.commit()
    assert out.verification_status == "VERIFIED"


def test_repo_verify_blocks_on_held_covered_month_close_row_lock(
    alembic_config, fresh_engine
):
    """Proof verify FOR UPDATE-locks existing close rows across the covered range.

    Connection A holds FOR UPDATE on the finance_month_close row for a covered
    month (2026-03) inside a link's range [2026-01, 2026-06]. A second Session
    runs verify under a short statement_timeout; because _require_range_open
    row-locks every EXISTING close row in the range with FOR UPDATE, it blocks on
    the held 2026-03 row and the lock wait is canceled. This is the serialization
    that stops a concurrent close from flipping an existing OPEN covered month to
    LOCKED between the scan and the verify/reject commit (PR #57 -bC / -iV).
    Reverting the range scan to a plain SELECT makes this test fail (no block).
    """
    from uuid import UUID, uuid4

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from ums_smart_revenue.finance.month_close import get_or_create_month_close_row

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)

    # Seed one UNVERIFIED link covering 2026-01..2026-06 and an existing OPEN close
    # row for the covered month 2026-03 (committed so holder/contender can see it).
    with Session(fresh_engine) as setup:
        link_id = (
            SqlAlchemyChannelAccountLinkRepository(setup, tenant_id=tenant)
            .propose_account_owner_link(
                adsense_account_id="pub-rng", content_owner_id="owner-rng",
                effective_month_start="2026-01", effective_month_end="2026-06",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )
            .id
        )
        get_or_create_month_close_row(setup, "2026-03", tenant_id=tenant)
        setup.commit()

    holder = fresh_engine.connect()
    holder_txn = holder.begin()
    try:
        # Hold the covered-month close-row lock so verify's range FOR UPDATE blocks.
        holder.execute(
            text(
                "SELECT 1 FROM finance_month_close WHERE tenant_id = :t "
                "AND month = '2026-03' FOR UPDATE"
            ),
            {"t": str(tenant)},
        )
        with Session(fresh_engine) as contender:
            # statement_timeout reliably cancels a blocked FOR UPDATE row-lock wait.
            contender.execute(text("SET statement_timeout = '750ms'"))
            repo = SqlAlchemyChannelAccountLinkRepository(contender, tenant_id=tenant)
            with pytest.raises(OperationalError) as exc_info:
                repo.verify_account_owner_link(link_id, verified_by=uuid4(), reason="x")
            err_msg = str(exc_info.value).lower()
            assert "canceling statement" in err_msg or "statement timeout" in err_msg, (
                "verify must be canceled by the covered-month row-lock wait timeout"
            )
    finally:
        holder_txn.rollback()
        holder.close()


def test_repo_derivation_blocks_on_held_observed_month_close_row_lock(
    alembic_config, fresh_engine
):
    """Proof derivation FOR UPDATE-locks existing close rows for observed months.

    Connection A holds FOR UPDATE on the finance_month_close row for an observed
    month (2026-03). A second Session runs upsert_owner_channel_links_from_source
    under a short statement_timeout; because the derivation row-locks every
    EXISTING close row for its observed months with FOR UPDATE before deciding
    which are LOCKED, it blocks on the held 2026-03 row and the lock wait is
    canceled. This is the serialization that stops a concurrent close from
    flipping an existing OPEN observed month to LOCKED between the derivation's
    read and its insert (PR #57 -GSk). Reverting the derivation's close-row scan
    to a plain (unlocked) SELECT makes this test fail (no block).
    """
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from ums_smart_revenue.finance.month_close import get_or_create_month_close_row

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)

    # Seed one source-row co-occurrence for observed month 2026-03 and an existing
    # OPEN close row for that month (committed so holder/contender can see both).
    with Session(fresh_engine) as setup:
        setup.add(
            GoogleRevenueSourceRowORM(
                tenant_id=tenant, source_system="youtube_reporting",
                source_row_key="deriv-lock-key".ljust(64, "0"),
                source_account_id="pub-deriv", content_owner_id="owner-deriv",
                youtube_channel_id="chan-deriv", report_month="2026-03",
                report_type="channel_basic_a2",
                period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
                period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
                metric_key="estimated_partner_revenue", value_kind="estimated",
                amount_native=0, currency_code="USD", raw_payload={},
            )
        )
        get_or_create_month_close_row(setup, "2026-03", tenant_id=tenant)
        setup.commit()

    holder = fresh_engine.connect()
    holder_txn = holder.begin()
    try:
        # Hold the observed-month close-row lock so the derivation FOR UPDATE blocks.
        holder.execute(
            text(
                "SELECT 1 FROM finance_month_close WHERE tenant_id = :t "
                "AND month = '2026-03' FOR UPDATE"
            ),
            {"t": str(tenant)},
        )
        with Session(fresh_engine) as contender:
            # statement_timeout reliably cancels a blocked FOR UPDATE row-lock wait.
            contender.execute(text("SET statement_timeout = '750ms'"))
            repo = SqlAlchemyChannelAccountLinkRepository(contender, tenant_id=tenant)
            with pytest.raises(OperationalError) as exc_info:
                repo.upsert_owner_channel_links_from_source()
            err_msg = str(exc_info.value).lower()
            assert "canceling statement" in err_msg or "statement timeout" in err_msg, (
                "derivation must be canceled by the observed-month row-lock wait timeout"
            )
    finally:
        holder_txn.rollback()
        holder.close()


def test_repo_derivation_blocks_on_held_absent_observed_month_advisory_lock(
    alembic_config, fresh_engine
):
    """Proof derivation get-or-creates + locks the close row for an ABSENT month.

    Connection A holds the per-(tenant, month) advisory lock for an observed month
    that has NO finance_month_close row (2026-07). A second Session runs
    upsert_owner_channel_links_from_source under a short statement_timeout; because
    the derivation now get-or-creates + locks EVERY observed month under the same
    advisory guard the close path uses (get_or_create_month_close_row(for_update=
    True)) — not only existing rows — it blocks acquiring the held advisory lock and
    the wait is canceled. This closes the absent-month window (a concurrent close
    inserting a fresh LOCKED row for a month with no row at read time) on the
    derivation path (PR #57 -GSk, round 4). Reverting the derivation to lock only
    EXISTING close rows makes this test fail (the absent month is never locked).
    """
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from ums_smart_revenue.finance.month_close import _finance_month_advisory_lock_key

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)

    # Seed one source-row co-occurrence for observed month 2026-07 and NO close row
    # (the absent-month case the round-4 get-or-create guard must serialize).
    with Session(fresh_engine) as setup:
        setup.add(
            GoogleRevenueSourceRowORM(
                tenant_id=tenant, source_system="youtube_reporting",
                source_row_key="deriv-absent-key".ljust(64, "0"),
                source_account_id="pub-deriv", content_owner_id="owner-absent",
                youtube_channel_id="chan-absent", report_month="2026-07",
                report_type="channel_basic_a2",
                period_start=datetime(2026, 7, 1, tzinfo=UTC).date(),
                period_end=datetime(2026, 7, 31, tzinfo=UTC).date(),
                metric_key="estimated_partner_revenue", value_kind="estimated",
                amount_native=0, currency_code="USD", raw_payload={},
            )
        )
        setup.commit()

    lock_key = _finance_month_advisory_lock_key("2026-07", tenant)
    holder = fresh_engine.connect()
    holder_txn = holder.begin()
    try:
        # Hold the per-(tenant, month) advisory lock for the ABSENT observed month so
        # the derivation's get_or_create_month_close_row advisory acquire blocks.
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})
        with Session(fresh_engine) as contender:
            contender.execute(text("SET statement_timeout = '750ms'"))
            repo = SqlAlchemyChannelAccountLinkRepository(contender, tenant_id=tenant)
            with pytest.raises(OperationalError) as exc_info:
                repo.upsert_owner_channel_links_from_source()
            err_msg = str(exc_info.value).lower()
            assert "canceling statement" in err_msg or "statement timeout" in err_msg, (
                "derivation must be canceled waiting on the absent-month advisory lock"
            )
    finally:
        holder_txn.rollback()
        holder.close()
