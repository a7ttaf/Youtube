"""PostgreSQL companion: exercise the real lock path on a live engine.

Repeats five representative service scenarios against PostgreSQL to verify
the pg_advisory_xact_lock + SELECT ... FOR UPDATE primitive executes.
Does NOT add a competing-session blocking test (see spec Section 7.5).
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

# Reuse the postgres URL helper that lives in tests/db/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from _postgres_helpers import require_postgres_url  # noqa: E402

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ParsedSourceRow,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.source_models import CurrencyORM
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    # Resets PostgreSQL public schema for isolated integration tests.
    # Fail-closed: refuses to run if database name does not look like a test DB.
    db_name = make_url(postgres_url).database or ""
    if not (db_name.startswith("test_") or db_name.endswith("_test")):
        raise RuntimeError(
            f"Refusing destructive schema reset for non-test database: {db_name!r}"
        )
    # ============================================================================
    # Purpose: Provide a fresh SQLAlchemy engine with a clean `public` schema
    # for one finance PG normalizer integration test. Bounded by
    # `SET LOCAL lock_timeout = '30s'` so a contended reset (e.g. an orphan
    # `idle in transaction` connection from a prior killed/hung run on a
    # shared/reused cluster) fails fast with a diagnosable `LockNotAvailable`
    # error instead of hanging indefinitely.
    # Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`.
    # Standards: `SET LOCAL` is transaction-scoped (reverts on
    # `engine.begin()` commit), so it does not alter the existing per-test
    # `statement_timeout` configuration elsewhere. The `try/finally` wrapper
    # guarantees `engine.dispose()` runs even when the schema reset raises
    # — no leaked pool on the contended-reset failure path.
    # Blast Radius: None detected — test-harness fixture only; no product
    # code, no migration, no `alembic/env.py` change.
    # Connections:
    #   - File: tests/finance/test_google_source_normalizer_postgres.py ->
    #     the fail-closed DB-name check above is the only test-DB safety
    #     gate; this block only changes the fail-fast lock bound.
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


def _seed_pg(session, tenant_id, channel_id) -> None:
    # Seeds minimal tenant/currency/channel rows for normalizer PG tests.
    # Defensively checks for existing USD/EGP before inserting.
    session.add(TenantORM(id=tenant_id, slug=f"pg-{tenant_id.hex[:6]}", display_name="PG"))
    # PostgreSQL currencies seeded by migration but ensure USD is supported and activated:
    # (the migration in 20260523_0001 may already do this; this is defensive)
    existing_usd = session.get(CurrencyORM, "USD")
    if existing_usd is None:
        session.add(CurrencyORM(
            code="USD", numeric_code="840", name="US Dollar",
            minor_unit=2, is_supported=True, activated_at=datetime.now(UTC),
        ))
    existing_egp = session.get(CurrencyORM, "EGP")
    if existing_egp is None:
        session.add(CurrencyORM(
            code="EGP", numeric_code="818", name="Egyptian Pound",
            minor_unit=2, is_supported=True, activated_at=datetime.now(UTC),
        ))
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=tenant_id, youtube_channel_id=channel_id,
            channel_name="PG Ch", cms_status="INSIDE_CMS",
            revenue_required=True, active=True,
        )
    )
    session.flush()


def _pg_row(channel: str, seed: str, amount: str = "100.000000") -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system="youtube_reporting",
        source_row_key=(seed * 64)[:64],
        source_account_id=channel, content_owner_id=None,
        youtube_channel_id=channel,
        report_type="x", report_month="2026-04",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        metric_key="estimatedRevenue", value_kind="estimated",
        amount_native=Decimal(amount), currency_code="USD",
        source_report_id="r-1",
        raw_payload={"dimensions": {"country": "US"}},
    )


# Verifies CREATED path: single eligible USD row produces one fact via record_fact.
def test_pg_created_path(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_create")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [_pg_row("UC_pg_create", "a")], raw_file_id=None, imported_by=None,
        )
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.created) == 1


# Verifies UNCHANGED replay: second normalization of identical payload yields no new write.
def test_pg_unchanged_replay(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_replay")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [_pg_row("UC_pg_replay", "b")], raw_file_id=None, imported_by=None,
        )
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(second.unchanged) == 1
        assert len(second.created) == 0


# Verifies UPDATED path: changed amount produces an updated fact.
def test_pg_updated_path(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_upd")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(tenant_id, [_pg_row("UC_pg_upd", "c", "100.000000")],
                          raw_file_id=None, imported_by=None)
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        repo.upsert_many(tenant_id, [_pg_row("UC_pg_upd", "c", "250.000000")],
                          raw_file_id=None, imported_by=None)
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(second.updated) == 1


# Verifies non-USD rows are skipped with non_usd_currency reason and produce no facts.
def test_pg_non_usd_currency_skip(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_fx")
        row = _pg_row("UC_pg_fx", "f")
        from dataclasses import replace as dc_replace
        row = dc_replace(row, currency_code="EGP")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [row], raw_file_id=None, imported_by=None,
        )
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert any(s.reason.value == "non_usd_currency" for s in result.skipped)
        assert len(result.created) == 0


# Verifies LOCKED month gate: RevenueFactLockedMonthError raised before any writes.
def test_pg_locked_month_raises(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_locked")
        session.add(
            FinanceMonthCloseORM(
                tenant_id=tenant_id, month="2026-04",
                status="LOCKED", allocation_rule_payload={},
            )
        )
        session.commit()
        with pytest.raises(RevenueFactLockedMonthError):
            GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
                month="2026-04", actor_user_id=ACTOR_USER_ID,
            )
