# ============================================================================
# Purpose: Postgres-tier proof for the composed-read snapshot ruling — the
#   platform-lane session really runs REPEATABLE READ (begun before the first
#   source read, rejected when a transaction is already active, reset at
#   pool checkin), and the client-observable hazards of READ COMMITTED
#   composition are dead: a writer committing mid-read cannot tear the
#   payment-match totals, a month close committing mid-read cannot make
#   smart-alerts label pre-lock totals as a LOCKED month, a channel
#   committing mid-read cannot flip the missing-facts coverage alert against
#   money alerts built from the pre-commit snapshot, a channel moved
#   between companies mid-request cannot have its revenue attributed to its
#   former company (the org-derived selection re-resolves in-snapshot), a
#   channel dropped from a group roster mid-request cannot keep feeding the
#   group rollup (active membership re-reads on the snapshot, deny-only),
#   and a channel-scoped read admitted by an inherited org grant cannot be
#   served after the channel moved out of the granted unit (the deny-only
#   grant re-check covers channel targets — on the scope-parameterized
#   loaders, the per-channel facts/summary loaders, the issue-queue
#   selection, and the group member filter — and the group's active flag
#   re-reads on the snapshot).
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

from collections.abc import Collection, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.api.exports import (
    _build_finance_source_summaries_for_export,
    _FinanceExportSourceContext,
)
from ums_smart_revenue.api.revenue import _load_month_net_revenue
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    CommittedAllocationRunORM,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.read_snapshot import (
    ComposedReadSnapshotError,
    begin_composed_read_snapshot,
)
from ums_smart_revenue.db.report_models import ExportJobORM
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
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponent,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.manual_overrides import SqlAlchemyManualOverrideRepository
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.reports.exports import ExportJobEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

MONTH = "2026-03"
CHANNEL_ID = "channel-snapshot-pg"
LATE_CHANNEL_ID = "channel-snapshot-pg-late"
SECTOR_ID = UUID("00000000-0000-0000-0000-00000000d101")
SECTOR_B_ID = UUID("00000000-0000-0000-0000-00000000d102")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000d201")
COMPANY_B_ID = UUID("00000000-0000-0000-0000-00000000d202")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000d301")
LATE_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000d302")
USER_ID = UUID("00000000-0000-0000-0000-00000000d401")
GROUP_ID = UUID("00000000-0000-0000-0000-00000000d501")
EXPORT_ID = UUID("00000000-0000-0000-0000-00000000d601")

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
                "'month_bank_reconciliation', 'month_gap_explanation', "
                "'monthly_net_revenue_summary', 'revenue_recalculation_preview', "
                "'monthly_channel_revenue_fact', 'revenue_reconciliation_issue_queue', "
                "'export_job', 'audit_log_page')"
            )
        )
        conn.execute(sa.text("DELETE FROM finance_month_close WHERE month = :m"), {"m": MONTH})
        conn.execute(
            sa.text("DELETE FROM committed_allocation_runs WHERE month = :m"), {"m": MONTH}
        )
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
        conn.execute(sa.text("DELETE FROM export_jobs WHERE id = :e"), {"e": str(EXPORT_ID)})
        conn.execute(
            sa.text("DELETE FROM channel_group_members WHERE group_id = :g"),
            {"g": str(GROUP_ID)},
        )
        conn.execute(sa.text("DELETE FROM channel_groups WHERE id = :g"), {"g": str(GROUP_ID)})
        conn.execute(
            sa.text("DELETE FROM youtube_channels WHERE youtube_channel_id IN (:c, :late)"),
            {"c": CHANNEL_ID, "late": LATE_CHANNEL_ID},
        )
        conn.execute(
            sa.text(
                "DELETE FROM org_units WHERE id IN (:company, :company_b, :sector, :sector_b)"
            ),
            {
                "company": str(COMPANY_ID),
                "company_b": str(COMPANY_B_ID),
                "sector": str(SECTOR_ID),
                "sector_b": str(SECTOR_B_ID),
            },
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


def sector_auth_headers() -> dict[str, str]:
    """Finance-viewer headers scoped to the seeded sector (inherited channel access)."""
    return {
        **auth_headers(),
        "x-scope-type": "sector",
        "x-scope-id": str(SECTOR_ID),
    }


def _seed_sector_b_with_company_b(owner_engine: sa.Engine) -> None:
    """Seed a second sector holding company B, the move-out target for the pins."""
    with Session(owner_engine) as seeder:
        seeder.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_B_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV Sector B",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_B_ID,
                    parent_id=SECTOR_B_ID,
                    type="COMPANY",
                    name="TV Company B",
                    active=True,
                ),
            ]
        )
        seeder.commit()


def _move_channel_out_then_begin(owner_engine: sa.Engine, fired: dict[str, bool]):
    """Build a begin-hook that moves the channel into company B (sector B) first.

    The tenant-lane gates resolved containment at dependency time; committing
    the move here lands it deterministically between those gates and the
    snapshot begin — the exact window the deny-only snapshot re-checks close.
    """

    def _hook(session: Session) -> None:
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text(
                        "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                        "WHERE youtube_channel_id = :channel"
                    ),
                    {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    return _hook


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


def test_net_revenue_company_scope_attribution_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A channel moved to another company after authorization but before the
    snapshot begins must not have its revenue attributed to the old company:
    the org-derived selection set re-resolves on the same MVCC snapshot as
    the money rows it selects."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            OrgUnitORM(
                id=COMPANY_B_ID,
                parent_id=SECTOR_ID,
                type="COMPANY",
                name="TV Company B",
                active=True,
            )
        )
        seeder.commit()
    fired = {"done": False}

    def _move_then_begin(session: Session) -> None:
        # The tenant-lane org index was loaded at dependency time; committing
        # the move here lands it deterministically between that load and the
        # snapshot begin — the exact window the attribution re-resolution
        # closes.
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text(
                        "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                        "WHERE youtube_channel_id = :channel"
                    ),
                    {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_move_then_begin,
    ):
        response = client.get(
            f"/revenue/months/{MONTH}/net-revenue",
            params={"scope_type": "company", "scope_id": str(COMPANY_ID)},
            headers=auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["channel_count"] == 0


def test_recalculation_dry_run_scope_attribution_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A channel moved to another company after authorization but before the
    dry-run snapshot begins must not be previewed under its former company:
    the dry run re-resolves selection and the COMPANY_UNMAPPED readiness map
    on the same MVCC snapshot as the facts it counts."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            OrgUnitORM(
                id=COMPANY_B_ID,
                parent_id=SECTOR_ID,
                type="COMPANY",
                name="TV Company B",
                active=True,
            )
        )
        seeder.commit()
    fired = {"done": False}

    def _move_then_begin(session: Session) -> None:
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text(
                        "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                        "WHERE youtube_channel_id = :channel"
                    ),
                    {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_move_then_begin,
    ):
        response = client.post(
            "/revenue/recalculate",
            headers={**auth_headers(), "x-role": "finance_admin"},
            json={
                "month": MONTH,
                "allocation_method": "gross_revenue_proportional",
                "scope_type": "company",
                "scope_id": str(COMPANY_ID),
                "dry_run": True,
                "reason": "attribution snapshot pin",
            },
        )

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["source_summary"]["revenue_fact_count"] == 0


def test_net_revenue_selection_stays_within_the_authorized_set(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A channel moved INTO the requested company after authorization must not
    be selected: snapshot membership is intersected with the gate-time set, so
    a channel covered in neither database state is never served."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            OrgUnitORM(
                id=COMPANY_B_ID,
                parent_id=SECTOR_ID,
                type="COMPANY",
                name="TV Company B",
                active=True,
            )
        )
        seeder.commit()
    with owner_engine.begin() as conn:
        # Gate-time state: the channel belongs to company B, so company A's
        # authorized member set is empty.
        conn.execute(
            sa.text(
                "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                "WHERE youtube_channel_id = :channel"
            ),
            {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
        )
    fired = {"done": False}

    def _move_then_begin(session: Session) -> None:
        # Move the channel INTO company A between the gate-time resolution and
        # the snapshot begin: snapshot membership alone would now select it.
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text(
                        "UPDATE youtube_channels SET primary_org_unit_id = :company "
                        "WHERE youtube_channel_id = :channel"
                    ),
                    {"company": str(COMPANY_ID), "channel": CHANNEL_ID},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_move_then_begin,
    ):
        response = client.get(
            f"/revenue/months/{MONTH}/net-revenue",
            params={"scope_type": "company", "scope_id": str(COMPANY_ID)},
            headers=auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["channel_count"] == 0


def test_net_revenue_close_probe_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A month lock committing after the snapshot begins must not pair a fresh
    LOCKED probe with an older in-snapshot committed run: the close probe
    reads through the same snapshot as the run it selects, so the response
    falls back to live compute instead of mislabeling the stale run."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        # An older committed run left behind by a previous lock/unlock cycle;
        # the month itself is OPEN (no close row).
        seeder.add(
            CommittedAllocationRunORM(
                id=uuid4(),
                month=MONTH,
                commit_version=1,
                allocation_method="gross_revenue_proportional",
                idempotency_key="snapshot-close-probe-v1",
                request_fingerprint="snapshot-close-probe-v1",
                component_count=0,
                allocated_component_count=0,
                unallocated_component_count=0,
                allocated_total_usd=Decimal("0"),
                unallocated_total_usd=Decimal("0"),
                net_applicable_total_usd=Decimal("0"),
                reconciliation_total_usd=Decimal("0"),
                committed_by=USER_ID,
                reason="seeded stale run for the close-probe pin",
            )
        )
        seeder.commit()
    fired = {"done": False}
    original = SqlAlchemyDeductionComponentRepository.list_month_components

    def _interleaved(
        self: SqlAlchemyDeductionComponentRepository,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
        component_kinds: Collection[str] | None = None,
    ) -> list[DeductionComponent]:
        # Lands after the loader's snapshot began and before the allocation
        # resolver runs: a tenant-lane close probe would see this LOCKED while
        # the platform snapshot still holds only the stale v1 run.
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.add(FinanceMonthCloseORM(month=MONTH, status="LOCKED", locked_by=USER_ID))
                writer.commit()
        return original(
            self,
            month=month,
            youtube_channel_ids=youtube_channel_ids,
            component_kinds=component_kinds,
        )

    with patch.object(
        SqlAlchemyDeductionComponentRepository, "list_month_components", _interleaved
    ):
        response = client.get(f"/revenue/months/{MONTH}/net-revenue", headers=auth_headers())

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["allocation_source"] == "live_compute"


def test_scoped_read_refuses_reparented_target_on_the_snapshot(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """A sector-granted caller whose target company was reparented out of the
    granted sector before the snapshot must be refused (403), not served
    snapshot-era finance data under gate-era containment: the loader
    re-asserts grant coverage against the snapshot index, deny-only."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            OrgUnitORM(
                id=SECTOR_B_ID,
                parent_id=None,
                type="SECTOR",
                name="TV Sector B",
                active=True,
            )
        )
        seeder.commit()
    with owner_engine.begin() as conn:
        # The reparent lands before the loader runs, so the snapshot sees the
        # company outside the granted sector while the (bypassed) gate-time
        # state had it inside — the exact epoch mix the re-check refuses.
        conn.execute(
            sa.text("UPDATE org_units SET parent_id = :sector_b WHERE id = :company"),
            {"sector_b": str(SECTOR_B_ID), "company": str(COMPANY_ID)},
        )
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="snapshot-pg@example.com",
        direct_permissions=(
            PermissionGrant(
                permission=Permission.VIEW_REVENUE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
            PermissionGrant(
                permission=Permission.VIEW_CONFIDENCE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
        ),
    )
    # The direct call bypasses the HTTP middleware, so arm the tenant
    # contextvar the org-index loader and the lane hooks resolve against.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tenant_token = TENANT_CTX.set(
        Tenant(
            id=UUID(UMS_TENANT_ID),
            slug="ums",
            display_name="UMS",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    platform_session = build_platform_session_factory(pg_url)()
    try:
        with pytest.raises(HTTPException) as excinfo:
            _load_month_net_revenue(
                month=MONTH,
                currency="USD",
                user=user,
                target_scope=AccessScope.company(str(COMPANY_ID)),
                channel_ids={CHANNEL_ID},
                platform_session=platform_session,
                revenue_repository=SqlAlchemyRevenueFactRepository(platform_session),
                override_repository=SqlAlchemyManualOverrideRepository(platform_session),
                deduction_component_repository=SqlAlchemyDeductionComponentRepository(
                    platform_session
                ),
                link_repository=SqlAlchemyChannelAccountLinkRepository(platform_session),
                committed_repository=SqlAlchemyCommittedAllocationRepository(platform_session),
            )
        assert excinfo.value.status_code == 403
    finally:
        platform_session.rollback()
        platform_session.close()
        TENANT_CTX.reset(tenant_token)


def test_net_revenue_group_scope_membership_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A channel dropped from the requested group after authorization but
    before the snapshot begins must not keep feeding the group rollup: active
    membership re-reads on the same MVCC snapshot as the money rows it selects
    and is intersected with the gate-time authorized set, deny-only."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            ChannelGroupORM(
                id=GROUP_ID,
                name="Snapshot Group",
                group_type="CUSTOM_GROUP",
                active=True,
            )
        )
        # The member row references (tenant_id, group_id) through a composite
        # FK with no ORM relationship, so flush the group first.
        seeder.flush()
        seeder.add(ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_ROW_ID))
        seeder.commit()
    fired = {"done": False}

    def _drop_member_then_begin(session: Session) -> None:
        # The tenant-lane group registry resolved the roster at gate time;
        # committing the membership delete here lands it deterministically
        # between that resolution and the snapshot begin — the exact window
        # the snapshot-side membership re-read closes.
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text("DELETE FROM channel_group_members WHERE group_id = :g"),
                    {"g": str(GROUP_ID)},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_drop_member_then_begin,
    ):
        response = client.get(
            f"/revenue/months/{MONTH}/net-revenue",
            params={"scope_type": "group", "scope_id": str(GROUP_ID)},
            headers=auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["channel_count"] == 0


def test_channel_read_refuses_moved_channel_on_the_snapshot(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """A channel-scoped caller admitted by an inherited sector grant whose
    channel moved out of the granted sector before the snapshot must be
    refused (403), not served snapshot-era finance data under gate-era
    containment: the deny-only grant re-check covers channel targets, while
    direct channel grants keep passing on scope identity alone."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_B_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV Sector B",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_B_ID,
                    parent_id=SECTOR_B_ID,
                    type="COMPANY",
                    name="TV Company B",
                    active=True,
                ),
            ]
        )
        seeder.commit()
    with owner_engine.begin() as conn:
        # The move lands before the loader runs, so the snapshot sees the
        # channel under company B in sector B while the (bypassed) gate-time
        # state had it inside the granted sector — the same epoch mix the
        # reparented-target pin refuses, now on a CHANNEL target.
        conn.execute(
            sa.text(
                "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                "WHERE youtube_channel_id = :channel"
            ),
            {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
        )
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="snapshot-pg@example.com",
        direct_permissions=(
            PermissionGrant(
                permission=Permission.VIEW_REVENUE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
            PermissionGrant(
                permission=Permission.VIEW_CONFIDENCE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
        ),
    )
    # The direct call bypasses the HTTP middleware, so arm the tenant
    # contextvar the org-index loader and the lane hooks resolve against.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tenant_token = TENANT_CTX.set(
        Tenant(
            id=UUID(UMS_TENANT_ID),
            slug="ums",
            display_name="UMS",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    platform_session = build_platform_session_factory(pg_url)()
    try:
        with pytest.raises(HTTPException) as excinfo:
            _load_month_net_revenue(
                month=MONTH,
                currency="USD",
                user=user,
                target_scope=AccessScope.channel(CHANNEL_ID),
                channel_ids={CHANNEL_ID},
                platform_session=platform_session,
                revenue_repository=SqlAlchemyRevenueFactRepository(platform_session),
                override_repository=SqlAlchemyManualOverrideRepository(platform_session),
                deduction_component_repository=SqlAlchemyDeductionComponentRepository(
                    platform_session
                ),
                link_repository=SqlAlchemyChannelAccountLinkRepository(platform_session),
                committed_repository=SqlAlchemyCommittedAllocationRepository(platform_session),
            )
        assert excinfo.value.status_code == 403
    finally:
        platform_session.rollback()
        platform_session.close()
        TENANT_CTX.reset(tenant_token)


def test_channel_facts_listing_refuses_moved_channel_on_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A per-channel facts listing admitted by an inherited sector grant must
    be refused (403) when the channel moved out of the granted sector between
    the gate and the snapshot: the shared per-channel loader re-checks the
    channel target against the snapshot index, deny-only."""
    _seed_month(owner_engine)
    _seed_sector_b_with_company_b(owner_engine)
    fired = {"done": False}

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_move_channel_out_then_begin(owner_engine, fired),
    ):
        response = client.get(
            f"/revenue/channels/{CHANNEL_ID}/months/{MONTH}/facts",
            headers=sector_auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 403


def test_reconciliation_issue_queue_selection_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A channel moved out of the caller's granted sector after the visible
    set was computed but before the snapshot begins must not have its facts
    paged into the issue queue: the loader recomputes the covered sets on the
    snapshot index and intersects them with the gate-time set, deny-only —
    and the audit records the snapshot-EFFECTIVE scope count, not the stale
    gate-time set the recheck already narrowed away."""
    _seed_month(owner_engine)
    _seed_sector_b_with_company_b(owner_engine)
    with Session(owner_engine) as seeder:
        # A second, disagreeing source so the channel's preview carries issues
        # (the queue drops issue-free channels).
        seeder.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(),
                month=MONTH,
                youtube_channel_id=CHANNEL_ID,
                source_kind="YOUTUBE_ANALYTICS",
                source_report_id="analytics-report-snapshot",
                gross_revenue_usd=Decimal("700.00"),
                net_revenue_usd=None,
                views=250000,
                watch_time_minutes=Decimal("7200.50"),
                confidence_score=Decimal("0.9500"),
                imported_by=USER_ID,
            )
        )
        seeder.commit()
    fired = {"done": False}

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_move_channel_out_then_begin(owner_engine, fired),
    ):
        response = client.get(
            f"/revenue/months/{MONTH}/reconciliation-issues",
            headers=sector_auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["pagination"]["has_more"] is False
    with owner_engine.connect() as conn:
        scoped_channel_count = conn.execute(
            sa.text(
                "SELECT (details ->> 'scoped_channel_count')::int FROM audit_logs "
                "WHERE entity_type = 'revenue_reconciliation_issue_queue' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert scoped_channel_count == 0


def test_group_scope_member_containment_rides_the_snapshot(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """A group member that stays on the roster but moves out of the caller's
    granted sector between the gate and the snapshot must not keep feeding
    the group rollup: surviving members are re-checked against the snapshot
    index (direct channel grants keep passing on scope identity), deny-only.

    Exercised via a direct loader call (the reparent-pin idiom): the HTTP
    net-revenue route also gates finance-month VIEW_FINALIZED_PAYMENTS, which
    a single sector-scoped gateway role cannot carry, so the epoch mix is
    reproduced by committing the move before the loader runs under a
    sector-granted principal."""
    _seed_month(owner_engine)
    _seed_sector_b_with_company_b(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            ChannelGroupORM(
                id=GROUP_ID,
                name="Snapshot Group",
                group_type="CUSTOM_GROUP",
                active=True,
            )
        )
        # The member row references (tenant_id, group_id) through a composite
        # FK with no ORM relationship, so flush the group first.
        seeder.flush()
        seeder.add(ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_ROW_ID))
        seeder.commit()
    with owner_engine.begin() as conn:
        # The move lands before the loader runs, so the snapshot sees the
        # member under company B in sector B while the (bypassed) gate-time
        # state had it inside the granted sector — the roster keeps the
        # member, only its containment drifted.
        conn.execute(
            sa.text(
                "UPDATE youtube_channels SET primary_org_unit_id = :company_b "
                "WHERE youtube_channel_id = :channel"
            ),
            {"company_b": str(COMPANY_B_ID), "channel": CHANNEL_ID},
        )
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="snapshot-pg@example.com",
        direct_permissions=(
            PermissionGrant(
                permission=Permission.VIEW_REVENUE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
            PermissionGrant(
                permission=Permission.VIEW_CONFIDENCE,
                scope=AccessScope.sector(str(SECTOR_ID)),
            ),
        ),
    )
    # The direct call bypasses the HTTP middleware, so arm the tenant
    # contextvar the org-index loader and the lane hooks resolve against.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tenant_token = TENANT_CTX.set(
        Tenant(
            id=UUID(UMS_TENANT_ID),
            slug="ums",
            display_name="UMS",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    platform_session = build_platform_session_factory(pg_url)()
    try:
        summary, _, _ = _load_month_net_revenue(
            month=MONTH,
            currency="USD",
            user=user,
            target_scope=AccessScope.group(str(GROUP_ID)),
            channel_ids={CHANNEL_ID},
            platform_session=platform_session,
            revenue_repository=SqlAlchemyRevenueFactRepository(platform_session),
            override_repository=SqlAlchemyManualOverrideRepository(platform_session),
            deduction_component_repository=SqlAlchemyDeductionComponentRepository(
                platform_session
            ),
            link_repository=SqlAlchemyChannelAccountLinkRepository(platform_session),
            committed_repository=SqlAlchemyCommittedAllocationRepository(platform_session),
        )
        assert summary.channel_count == 0
    finally:
        platform_session.rollback()
        platform_session.close()
        TENANT_CTX.reset(tenant_token)


def test_group_scope_active_flag_rides_the_snapshot(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A group archived after the gate-time active check but before the
    snapshot begins must not keep serving its roster: the selection re-reads
    the group row on the snapshot and empties when it is inactive there."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            ChannelGroupORM(
                id=GROUP_ID,
                name="Snapshot Group",
                group_type="CUSTOM_GROUP",
                active=True,
            )
        )
        seeder.flush()
        seeder.add(ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=CHANNEL_ROW_ID))
        seeder.commit()
    fired = {"done": False}

    def _archive_then_begin(session: Session) -> None:
        # The gate-time resolver saw the group active; committing the archive
        # here lands it deterministically between that check and the snapshot
        # begin - the window the snapshot-side active re-read closes.
        if not fired["done"]:
            fired["done"] = True
            with Session(owner_engine) as writer:
                writer.execute(
                    sa.text("UPDATE channel_groups SET active = FALSE WHERE id = :g"),
                    {"g": str(GROUP_ID)},
                )
                writer.commit()
        begin_composed_read_snapshot(session)

    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        side_effect=_archive_then_begin,
    ):
        response = client.get(
            f"/revenue/months/{MONTH}/net-revenue",
            params={"scope_type": "group", "scope_id": str(GROUP_ID)},
            headers=auth_headers(),
        )

    assert fired["done"] is True
    assert response.status_code == 200
    assert response.json()["channel_count"] == 0


def test_finance_export_preview_composes_one_snapshot_under_concurrent_writer(
    owner_engine: sa.Engine, client: TestClient
) -> None:
    """A payment committing between the export builder's facts read and its
    payments read must not appear in the workbook preview: a persisted-bound
    export artifact composes the same finance sources as the dashboard reads,
    so its totals must come from one MVCC snapshot too."""
    _seed_month(owner_engine)
    with Session(owner_engine) as seeder:
        seeder.add(
            ExportJobORM(
                id=EXPORT_ID,
                export_type="FINANCE_EXCEL",
                scope_type="global",
                scope_id=None,
                month=MONTH,
                currency="USD",
                requested_by=USER_ID,
                status="QUEUED",
                month_lock_status="OPEN",
                include_confidence_notes=True,
                include_manual_override_notes=True,
            )
        )
        seeder.commit()
    fired = {"done": False}
    original = SqlAlchemyAdSensePaymentRepository.list_month_payments

    def _interleaved(
        self: SqlAlchemyAdSensePaymentRepository, *, month: str
    ) -> list[AdSensePaymentEntry]:
        # Lands between the builder's facts read and its payments read - the
        # exact READ COMMITTED window that pairs fresh payment totals with
        # stale fact totals inside a downloadable artifact.
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
                        raw_payload={"paymentId": "pay-snapshot-late-export"},
                        source_report_id="adsense-payment-snapshot-late-export",
                        source_account_id="pub-1",
                        imported_by=USER_ID,
                    )
                )
                writer.commit()
        return original(self, month=month)

    with patch.object(SqlAlchemyAdSensePaymentRepository, "list_month_payments", _interleaved):
        response = client.get(
            f"/exports/{EXPORT_ID}/finance-workbook-preview",
            headers={**auth_headers(), "x-role": "finance_admin"},
        )

    assert fired["done"] is True
    assert response.status_code == 200
    payment_match = response.json()["source_summaries"]["payment_match"]
    assert Decimal(payment_match["youtube_revenue_total_usd"]) == Decimal("930.00")
    assert Decimal(payment_match["adsense_paid_amount"]) == Decimal("900.00")


def test_finance_export_builder_releases_the_snapshot_before_artifact_work(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """The builder must end its REPEATABLE READ transaction when the source
    reads complete: artifact byte generation and filesystem persistence run
    AFTER it returns and must not hold the snapshot open (idle-in-transaction
    timeouts, vacuum pressure) - the platform session is request-scoped and
    would otherwise keep the transaction until teardown."""
    _seed_month(owner_engine)
    export_job = ExportJobEntry(
        id="exp-snapshot-release",
        export_type="FINANCE_EXCEL",
        scope_type="global",
        scope_id=None,
        month=MONTH,
        currency="USD",
        requested_by=str(USER_ID),
        status="QUEUED",
        file_url=None,
        month_lock_status="OPEN",
        include_confidence_notes=True,
        include_manual_override_notes=True,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        completed_at=None,
        scope_channel_ids=None,
    )
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="snapshot-pg@example.com",
        direct_permissions=(),
    )
    # The direct call bypasses the HTTP middleware, so arm the tenant
    # contextvar the finance repositories resolve against.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tenant_token = TENANT_CTX.set(
        Tenant(
            id=UUID(UMS_TENANT_ID),
            slug="ums",
            display_name="UMS",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    platform_session = build_platform_session_factory(pg_url)()
    try:
        summaries = _build_finance_source_summaries_for_export(
            context=_FinanceExportSourceContext(
                export_job=export_job,
                user=user,
                # The tenant-lane session is only touched for audit-derived
                # alert reads, disabled here; the platform session stands in.
                session=platform_session,
                platform_session=platform_session,
                org_index=OrgAccessIndex(),
                group_registry=ChannelGroupRegistry(),
                include_audit_derived_alerts=False,
            ),
        )
        assert summaries.net_revenue.month == MONTH
        assert platform_session.in_transaction() is False
    finally:
        platform_session.rollback()
        platform_session.close()
        TENANT_CTX.reset(tenant_token)
