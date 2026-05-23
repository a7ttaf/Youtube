"""PostgreSQL-backed round-trip test for 20260523_0001.

upgrade head (20260521_0001) -> upgrade 20260523_0001 -> downgrade -1
-> upgrade head again. Verifies idempotency and that downgrade truly
reverses the upgrade.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from _postgres_helpers import POSTGRES_URL  # sibling module (pytest's prepend mode puts tests/db on sys.path)
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows import (
    ParsedSourceRow,
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.tenant_models import TenantORM

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_config() -> Config:
    # Build Config WITHOUT passing the alembic.ini path. env.py only calls
    # `logging.config.fileConfig()` when `config.config_file_name is not None`.
    # `fileConfig` defaults to `disable_existing_loggers=True`, which silences
    # pytest's `caplog` fixture for every later test in the session and breaks
    # unrelated tenancy log-assertion tests (`tests/tenancy/test_resolver.py`).
    # Setting `script_location` and `sqlalchemy.url` directly gives alembic
    # everything it needs without touching the logging tree.
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", POSTGRES_URL)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine() -> object:
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_pre_state_at_prior_head(alembic_config: Config, fresh_engine: object) -> None:
    command.upgrade(alembic_config, "20260521_0001")
    inspector = inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    assert "currency_exchange_rates" in tables, (
        "legacy table must be present from earlier revision"
    )
    assert "currencies" not in tables
    assert "google_revenue_source_rows" not in tables


def test_upgrade_creates_currencies_and_source_rows(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, "20260523_0001")
    inspector = inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    assert "currencies" in tables
    assert "google_revenue_source_rows" in tables
    assert "currency_exchange_rates" in tables  # legacy preserved per spec §6

    # Currencies seeded with v1 supported flip.
    with fresh_engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM currencies")).scalar_one()
        assert count >= 150
        supported = (
            conn.execute(
                text(
                    "SELECT code FROM currencies WHERE is_supported = true ORDER BY code"
                )
            )
            .scalars()
            .all()
        )
        assert supported == ["AED", "EGP", "EUR", "GBP", "SAR", "USD"]


def test_indexes_present_on_google_revenue_source_rows(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, "20260523_0001")
    inspector = inspect(fresh_engine)
    indexes = {i["name"] for i in inspector.get_indexes("google_revenue_source_rows")}
    assert "ix_google_revenue_source_rows_tenant_month_source" in indexes
    assert "ix_google_revenue_source_rows_tenant_channel_month" in indexes


def test_partial_channel_month_index_has_where_clause(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, "20260523_0001")
    with fresh_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ix_google_revenue_source_rows_tenant_channel_month'"
            )
        ).scalar_one()
        assert "WHERE" in row.upper()
        assert "youtube_channel_id" in row.lower()


def test_downgrade_drops_only_b1_tables(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, "20260523_0001")
    command.downgrade(alembic_config, "-1")
    inspector = inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    assert "currencies" not in tables
    assert "google_revenue_source_rows" not in tables
    assert "currency_exchange_rates" in tables  # untouched


def test_round_trip_idempotency(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, "20260523_0001")
    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "20260523_0001")
    # Should not raise; re-upgrade must succeed cleanly.
    inspector = inspect(fresh_engine)
    assert "currencies" in inspector.get_table_names()


def test_repository_upsert_round_trip_on_postgres(
    alembic_config: Config, fresh_engine: object
) -> None:
    """Exercise the production upsert path on real PostgreSQL —
    `postgresql.insert(...).on_conflict_do_update(...)` — which the SQLite
    repository suite cannot reach. Covers insert, conflict-update of a mutable
    field, and id preservation across the conflict.
    """
    command.upgrade(alembic_config, "20260523_0001")
    tenant_id = uuid4()

    def _row(amount: str) -> ParsedSourceRow:
        return ParsedSourceRow(
            source_system="youtube_reporting",
            source_row_key="p" * 64,
            source_account_id="acct-pg",
            content_owner_id=None,
            youtube_channel_id="UC_pg",
            report_type="channel_monthly_estimated_revenue",
            report_month="2026-04",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            metric_key="estimatedRevenue",
            value_kind="estimated",
            amount_native=Decimal(amount),
            currency_code="USD",  # seeded + flipped to supported by the migration
            source_report_id="r-pg",
            raw_payload={"dimensions": {"country": "US"}},
        )

    with Session(fresh_engine) as session:
        session.add(TenantORM(id=tenant_id, slug="pg-tenant", display_name="PG Tenant"))
        session.flush()
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

        first = repo.upsert_many(
            tenant_id, [_row("100.000000")], raw_file_id=None, imported_by=None
        )
        second = repo.upsert_many(
            tenant_id, [_row("150.000000")], raw_file_id=None, imported_by=None
        )
        session.commit()

        assert first[0].id == second[0].id, "id must be preserved across the on-conflict update"
        rows = repo.list(tenant_id, report_month="2026-04")
        assert len(rows) == 1, "conflict must update in place, not insert a duplicate"
        assert rows[0].amount_native == Decimal("150.000000")
