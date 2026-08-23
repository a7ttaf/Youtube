# ============================================================================
# Purpose: Postgres-tier proof for the composed-read snapshot ruling — the
#   platform-lane session really runs REPEATABLE READ (begun before the first
#   source read, rejected when a transaction is already active, reset at
#   pool checkin), and the client-observable hazards of READ COMMITTED
#   composition are dead: a writer committing mid-read cannot tear the
#   payment-match totals, a month close committing mid-read cannot make
#   smart-alerts label pre-lock totals as a LOCKED month, and a channel
#   committing mid-read cannot flip the missing-facts coverage alert against
#   money alerts built from the pre-commit snapshot.
# Database/ORM: PostgreSQL only (transaction isolation is the subject);
#   requires UMS_TEST_DATABASE_URL — require_postgres_url() raises, never
#   skips, preserving the no-skip policy. Seeds finance rows through the
#   RLS-bypassing owner engine with the UMS tenant server_default.
# Standards: The mid-read writer is injected by wrapping one repository read
#   so the concurrent commit lands deterministically between two source
#   fetches of the same request — no sleeps, no races. Assertions observe
#   the wire payload, not repository internals.
# Blast Radius: Test-only, but failing here means composed finance responses
#   can publish totals that never coexisted in the database.
# Connections:
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the helper under
#     proof.
#   - File: backend/ums_smart_revenue/api/revenue.py -> the composed reads
#     whose snapshot semantics are pinned.
#   - File: tests/api/test_composed_read_snapshot_wiring.py -> the SQLite
#     tier pinning that every composed read calls the helper.
# ============================================================================
"""Postgres-tier proof: composed finance reads compose one MVCC snapshot."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.read_snapshot import (
    ComposedReadSnapshotError,
    begin_composed_read_snapshot,
)
from ums_smart_revenue.db.security_models import UserORM
from ums_smart_revenue.db.session import build_platform_session_factory
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentEntry,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationEntry,
    SqlAlchemyBankReconciliationRepository,
)

MONTH = "2026-03"
CHANNEL_ID = "channel-snapshot-pg"
LATE_CHANNEL_ID = "channel-snapshot-pg-late"
SECTOR_ID = UUID("00000000-0000-0000-0000-00000000d101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000d201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000d301")
LATE_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000d302")
USER_ID = UUID("00000000-0000-0000-0000-00000000d401")

_UPGRADED_URLS: set[str] = set()


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without an ini file."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


def _ensure_upgraded(url: str) -> None:
    """Migrate the disposable Postgres database to head once per session."""
    if url in _UPGRADED_URLS:
        return
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED_URLS.add(url)


def _purge_test_rows(engine: sa.Engine) -> None:
    """Remove this module's finance/org/audit rows so reruns stay idempotent."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM audit_logs WHERE entity_type IN "
                "('monthly_payment_match', 'monthly_smart_alerts', "
                "'month_bank_reconciliation', 'month_gap_explanation')"
            )
        )
        conn.execute(sa.text("DELETE FROM finance_month_close WHERE month = :m"), {"m": MONTH})
        conn.execute(sa.text("DELETE FROM adsense_payments WHERE month = :m"), {"m": MONTH})
        conn.execute(
            sa.text("DELETE FROM bank_reconciliation_entries WHERE month = :m"), {"m": MONTH}
        )
        conn.execute(
            sa.text(
                "DELETE FROM monthly_channel_revenue_facts WHERE youtube_channel_id = :c"
            ),
            {"c": CHANNEL_ID},
        )
        conn.execute(
            sa.text("DELETE FROM youtube_channels WHERE youtube_channel_id IN (:c, :late)"),
            {"c": CHANNEL_ID, "late": LATE_CHANNEL_ID},
        )
        conn.execute(
            sa.text("DELETE FROM org_units WHERE id IN (:company, :sector)"),
            {"company": str(COMPANY_ID), "sector": str(SECTOR_ID)},
        )
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": str(USER_ID)})


def _seed_month(engine: sa.Engine) -> None:
    """Seed one month: facts 930 / paid 900 + pending 30 / bank 880, no close."""
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id=CHANNEL_ID,
                    channel_name="Snapshot PG",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
            ]
        )
        # The facts row references (tenant_id, youtube_channel_id) through a
        # composite FK with no ORM relationship, so flush the channel first —
        # the unit of work has no dependency edge to order these inserts.
        session.flush()
        session.add_all(
            [
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month=MONTH,
                    youtube_channel_id=CHANNEL_ID,
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-snapshot",
                    gross_revenue_usd=Decimal("930.00"),
                    net_revenue_usd=None,
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9825"),
                    imported_by=USER_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month=MONTH,
                    payment_name="AdSense payment March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("900.00"),
                    payment_currency="USD",
                    payment_status="PAID",
                    raw_payload={"paymentId": "pay-snapshot-paid"},
                    source_report_id="adsense-payment-snapshot",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month=MONTH,
                    payment_name="AdSense pending March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("30.00"),
                    payment_currency="USD",
                    payment_status="PENDING",
                    raw_payload={"paymentId": "pay-snapshot-pending"},
                    source_report_id="adsense-payment-snapshot",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
                BankReconciliationEntryORM(
                    id=uuid4(),
                    month=MONTH,
                    bank_reference="bank-transfer-snapshot",
                    bank_received_date=date(2026, 4, 22),
                    bank_received_amount=Decimal("880.00"),
                    bank_received_currency="USD",
                    bank_received_amount_usd=Decimal("880.00"),
                    transfer_fee_usd=Decimal("12.00"),
                    fx_difference_usd=Decimal("5.00"),
                    notes=None,
                    source_report_id="bank-statement-snapshot",
                    recorded_by=USER_ID,
                ),
                UserORM(
                    id=USER_ID,
                    email="snapshot-pg@example.com",
                    display_name="Snapshot PG User",
                ),
            ]
        )
        session.commit()


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL, migrated to head."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    return url


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an RLS-bypassing owner engine, purging test rows either side."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_test_rows(engine)
        yield engine
        _purge_test_rows(engine)
    finally:
        engine.dispose()


@pytest.fixture
def client(pg_url: str) -> TestClient:
    """Build a Postgres-backed trusted-header client."""
    return TestClient(create_app(database_url=pg_url, authz_source="headers"))


def auth_headers() -> dict[str, str]:
    """Build finance-viewer trusted-gateway headers for the composed reads."""
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": "snapshot-pg@example.com",
        "x-role": "finance_viewer",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def test_begin_composed_read_snapshot_runs_repeatable_read(pg_url: str) -> None:
    """The helper begins the platform session's transaction at REPEATABLE READ."""
    session = build_platform_session_factory(pg_url)()
    try:
        begin_composed_read_snapshot(session)
        isolation = session.execute(
            sa.text("SELECT current_setting('transaction_isolation')")
        ).scalar_one()
        assert isolation == "repeatable read"
    finally:
        session.rollback()
        session.close()


def test_begin_composed_read_snapshot_rejects_active_transaction(pg_url: str) -> None:
    """An already-begun transaction cannot adopt the snapshot: fail loudly."""
    session = build_platform_session_factory(pg_url)()
    try:
        session.execute(sa.text("SELECT 1"))
        with pytest.raises(ComposedReadSnapshotError):
            begin_composed_read_snapshot(session)
    finally:
        session.rollback()
        session.close()


def test_snapshot_isolation_resets_after_the_transaction(pg_url: str) -> None:
    """REPEATABLE READ never leaks into later transactions on the pooled
    connection: after the snapshot transaction ends, the next one is back on
    the engine default."""
    session = build_platform_session_factory(pg_url)()
    try:
        begin_composed_read_snapshot(session)
        session.execute(sa.text("SELECT 1"))
        session.commit()
        isolation = session.execute(
            sa.text("SELECT current_setting('transaction_isolation')")
        ).scalar_one()
        assert isolation == "read committed"
    finally:
        session.rollback()
        session.close()


def test_payment_match_composes_one_snapshot_under_concurrent_writer(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A payment committing between the facts read and the payments read must
    not appear in the response: both totals come from one MVCC snapshot."""
    _seed_month(owner_engine)
    fired = {"done": False}
    original = SqlAlchemyAdSensePaymentRepository.list_month_payments

    def _interleaved(
        self: SqlAlchemyAdSensePaymentRepository, *, month: str
    ) -> list[AdSensePaymentEntry]:
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.add(
                    AdSensePaymentORM(
                        id=uuid4(),
                        month=MONTH,
                        payment_name="AdSense late arrival",
                        payment_date=date(2026, 4, 23),
                        payment_amount=Decimal("100.00"),
                        payment_currency="USD",
                        payment_status="PAID",
                        raw_payload={"paymentId": "pay-snapshot-late"},
                        source_report_id="adsense-payment-snapshot-late",
                        source_account_id="pub-1",
                        imported_by=USER_ID,
                    )
                )
                writer.commit()
        return original(self, month=month)

    with patch.object(SqlAlchemyAdSensePaymentRepository, "list_month_payments", _interleaved):
        response = client.get(f"/revenue/months/{MONTH}/payment-match", headers=auth_headers())

    assert fired["done"] is True
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["youtube_revenue_total_usd"]) == Decimal("930.00")
    assert Decimal(body["adsense_paid_amount"]) == Decimal("900.00")


def test_smart_alerts_close_transition_cannot_mislabel_locked(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A month close committing mid-read must not suppress MONTH_NOT_LOCKED:
    the close status pairs with the pre-lock totals of the same snapshot."""
    _seed_month(owner_engine)
    fired = {"done": False}
    original = SqlAlchemyBankReconciliationRepository.list_month_entries

    def _interleaved(
        self: SqlAlchemyBankReconciliationRepository, *, month: str
    ) -> list[BankReconciliationEntry]:
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.add(FinanceMonthCloseORM(month=MONTH, status="LOCKED", locked_by=USER_ID))
                writer.commit()
        return original(self, month=month)

    with patch.object(SqlAlchemyBankReconciliationRepository, "list_month_entries", _interleaved):
        response = client.get(f"/revenue/months/{MONTH}/smart-alerts", headers=auth_headers())

    assert fired["done"] is True
    assert response.status_code == 200
    alerts = {alert["code"]: alert for alert in response.json()["alerts"]}
    assert "MONTH_NOT_LOCKED" in alerts
    assert alerts["MONTH_NOT_LOCKED"]["details"]["close_status"] == "OPEN"


def test_smart_alerts_coverage_pairs_with_the_snapshot_facts(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """An active revenue-required channel committing mid-read must not flip the
    missing-facts coverage alert: the coverage query is not audit-gated, so it
    reads inside the same snapshot as the facts it is compared against."""
    _seed_month(owner_engine)
    fired = {"done": False}
    original = SqlAlchemyBankReconciliationRepository.list_month_entries

    def _interleaved(
        self: SqlAlchemyBankReconciliationRepository, *, month: str
    ) -> list[BankReconciliationEntry]:
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.add(
                    YouTubeChannelORM(
                        id=LATE_CHANNEL_ROW_ID,
                        youtube_channel_id=LATE_CHANNEL_ID,
                        channel_name="Snapshot PG Late",
                        primary_org_unit_id=COMPANY_ID,
                        cms_status="INSIDE_CMS",
                        revenue_required=True,
                        active=True,
                    )
                )
                writer.commit()
        return original(self, month=month)

    with patch.object(SqlAlchemyBankReconciliationRepository, "list_month_entries", _interleaved):
        response = client.get(f"/revenue/months/{MONTH}/smart-alerts", headers=auth_headers())

    assert fired["done"] is True
    assert response.status_code == 200
    codes = {alert["code"] for alert in response.json()["alerts"]}
    assert "CHANNELS_MISSING_REVENUE_FACTS" not in codes
