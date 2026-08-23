# ============================================================================
# Purpose: API pins for GET /revenue/months/{month}/gap-explanation — the
#   four-gate permission set (the smart-alerts set: revenue + confidence @
#   global, payments + bank @ finance month), the USD-only currency gate, the
#   atomic triple audit, close-status passthrough, and the response-shape
#   golden.
# Database/ORM: SQLite-tier TestClient tests over the real app factory; the
#   gate-isolation tests call the route function directly with hand-built
#   principals (the payment-match 422 idiom) because no seeded role splits
#   these view permissions.
# Standards: Exact-string assertions on permission denials and wire keys;
#   audit rows verified in the database, not just the response body.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> the route under test.
#   - File: tests/finance/test_gap_explanation.py -> the builder-level pins.
# ============================================================================
"""API tests for the month gap-explanation endpoint."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.revenue import get_month_gap_explanation
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000b101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000b201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000b301")
USER_ID = UUID("00000000-0000-0000-0000-00000000b401")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Create authentication headers for a user with specified role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "gap-explanation@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path: Path) -> str:
    """Build and return a new SQLite database URL in the provided temporary path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    """Seed one month whose chain decomposes into both gap legs.

    Facts 930 / paid 900 + pending 30 / bank 880 with fee 12 and FX 5:
    the payment leg is fully explained, the bank leg partially (residual 3).
    """
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("930.00"),
                    net_revenue_usd=None,
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9825"),
                    imported_by=USER_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month="2026-03",
                    payment_name="AdSense payment March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("900.00"),
                    payment_currency="USD",
                    payment_status="PAID",
                    raw_payload={"paymentId": "pay_2026_03"},
                    source_report_id="adsense-payment-2026-03",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month="2026-03",
                    payment_name="AdSense pending March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("30.00"),
                    payment_currency="USD",
                    payment_status="PENDING",
                    raw_payload={"paymentId": "pay_2026_03_pending"},
                    source_report_id="adsense-payment-2026-03",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
                BankReconciliationEntryORM(
                    id=uuid4(),
                    month="2026-03",
                    bank_reference="bank-transfer-2026-04-22",
                    bank_received_date=date(2026, 4, 22),
                    bank_received_amount=Decimal("880.00"),
                    bank_received_currency="USD",
                    bank_received_amount_usd=Decimal("880.00"),
                    transfer_fee_usd=Decimal("12.00"),
                    fx_difference_usd=Decimal("5.00"),
                    notes=None,
                    source_report_id="bank-statement-2026-04",
                    recorded_by=USER_ID,
                ),
                UserORM(
                    id=USER_ID,
                    email="gap-explanation@example.com",
                    display_name="Gap Explanation User",
                ),
            ]
        )
        if locked_month:
            session.add(
                FinanceMonthCloseORM(
                    month="2026-03",
                    status="LOCKED",
                    locked_by=USER_ID,
                )
            )
        session.commit()


def test_finance_viewer_reads_gap_explanation_with_triple_audit(tmp_path):
    """The golden read: wire shape, leg decomposition, and the audit triple."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/gap-explanation",
        headers=auth_headers("finance_viewer", "global"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.event_type)).all()

    assert response.status_code == 200
    body = response.json()
    assert list(body) == [
        "month",
        "currency",
        "close_status",
        "status",
        "tolerance_usd",
        "payment_leg",
        "bank_leg",
        "warnings",
        "money_provenance",
        "narrative",
        "audit_events",
    ]
    assert list(body["payment_leg"]) == [
        "status",
        "youtube_revenue_total_usd",
        "adsense_paid_amount_usd",
        "payment_gap_usd",
        "payment_match_status",
        "components",
        "unexplained_residual_usd",
        "unexplained_residual_confidence",
        "narrative",
    ]
    assert list(body["bank_leg"]) == [
        "status",
        "adsense_paid_amount_usd",
        "bank_received_amount_usd",
        "bank_gap_usd",
        "bank_reconciliation_status",
        "components",
        "unexplained_residual_usd",
        "unexplained_residual_confidence",
        "narrative",
    ]
    assert body["month"] == "2026-03"
    assert body["currency"] == "USD"
    assert body["close_status"] == "OPEN"
    assert body["status"] == "PARTIALLY_EXPLAINED"
    assert body["tolerance_usd"] == "0.01"

    payment_leg = body["payment_leg"]
    assert payment_leg["status"] == "FULLY_EXPLAINED"
    assert payment_leg["youtube_revenue_total_usd"] == "930"
    assert payment_leg["adsense_paid_amount_usd"] == "900"
    assert payment_leg["payment_gap_usd"] == "30"
    assert payment_leg["payment_match_status"] == "PAYMENT_VARIANCE"
    assert payment_leg["components"][0]["key"] == "non_paid_adsense_payments"
    assert payment_leg["components"][0]["amount_usd"] == "30"
    assert payment_leg["unexplained_residual_usd"] == "0"
    assert payment_leg["unexplained_residual_confidence"] == {"label": "HIGH", "score": "0.95"}

    bank_leg = body["bank_leg"]
    assert bank_leg["status"] == "PARTIALLY_EXPLAINED"
    assert bank_leg["bank_gap_usd"] == "20"
    assert bank_leg["bank_reconciliation_status"] == "BANK_VARIANCE"
    assert {c["key"]: c["amount_usd"] for c in bank_leg["components"]} == {
        "transfer_fee": "12",
        "fx_difference": "5",
    }
    assert bank_leg["unexplained_residual_usd"] == "3"
    assert bank_leg["unexplained_residual_confidence"] == {"label": "LOW", "score": "0"}

    assert [event["event_type"] for event in body["audit_events"]] == [
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
        "BANK_RECONCILIATION_VIEWED",
    ]
    audit_logs_by_type = {log.event_type: log for log in audit_logs}
    assert set(audit_logs_by_type) == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
        "BANK_RECONCILIATION_VIEWED",
    }
    assert all(log.entity_type == "month_gap_explanation" for log in audit_logs)
    assert all(log.sensitive is True for log in audit_logs)
    assert (
        audit_logs_by_type["REVENUE_VIEWED"].scope_type,
        audit_logs_by_type["REVENUE_VIEWED"].scope_id,
    ) == ("global", None)
    for month_scoped in ("PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED"):
        assert (
            audit_logs_by_type[month_scoped].scope_type,
            audit_logs_by_type[month_scoped].scope_id,
        ) == ("finance-month", "2026-03")


class _CountingEmptyRepository:
    """Source repository stub returning empty months and counting fetches."""

    def __init__(self) -> None:
        self.calls = 0

    def _count(self) -> list[object]:
        self.calls += 1
        return []

    def list_month_facts(self, *, month: str) -> list[object]:
        """Return no facts; count the fetch."""
        return self._count()

    def list_month_payments(self, *, month: str) -> list[object]:
        """Return no payments; count the fetch."""
        return self._count()

    def list_month_entries(self, *, month: str) -> list[object]:
        """Return no bank entries; count the fetch."""
        return self._count()


class _FlippingCloseRepository:
    """Close repository stub that transitions OPEN -> LOCKED between reads."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, month: str) -> object | None:
        """Return no close row first, then a LOCKED close."""
        self.calls += 1
        if self.calls == 1:
            return None
        return SimpleNamespace(status="LOCKED")


def test_close_transition_mid_read_refetches_sources_once():
    """A close committing mid-read must not label pre-lock totals LOCKED.

    The loader reads the close status before and after the source fetches;
    on a detected transition it refetches the sources exactly once so the
    reported close state pairs with the totals it actually froze.
    """
    from ums_smart_revenue.api.revenue import _load_month_gap_explanation

    revenue_repository = _CountingEmptyRepository()
    payment_repository = _CountingEmptyRepository()
    bank_repository = _CountingEmptyRepository()
    close_repository = _FlippingCloseRepository()

    explanation = _load_month_gap_explanation(
        month="2026-03",
        currency="USD",
        # SQLite in-memory session: the composed-read snapshot begin the
        # loader now owns is a no-op off Postgres, keeping this a pure
        # retry-semantics pin.
        platform_session=Session(create_engine("sqlite+pysqlite:///:memory:")),
        revenue_repository=revenue_repository,
        payment_repository=payment_repository,
        bank_repository=bank_repository,
        close_repository=close_repository,
    )

    assert explanation.close_status == "LOCKED"
    # One refetch each: the initial read plus the post-transition retry.
    assert revenue_repository.calls == 2
    assert payment_repository.calls == 2
    assert bank_repository.calls == 2
    # Close reads: the pre-read plus one per source-fetch round.
    assert close_repository.calls == 3


class _ThirdAppendFailsSink(InMemoryAuditSink):
    """Sink whose third append raises, staged inside the real boundary."""

    def __init__(self) -> None:
        super().__init__()
        self._appends = 0

    def append(self, record) -> None:  # noqa: ANN001 - protocol shape
        self._appends += 1
        if self._appends == 3:
            raise RuntimeError("staged third-append failure")
        super().append(record)


def test_failed_third_audit_append_retracts_the_whole_triple(tmp_path):
    """The audit triple lands atomically: a late append failure keeps nothing.

    The three records disclose ONE composed read, so a failure on the third
    append must retract the first two via the sink's transaction boundary —
    no partial audit triple may describe a response that was never returned.
    """
    from ums_smart_revenue.api.revenue import current_revenue_audit_sink

    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    failing_sink = _ThirdAppendFailsSink()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_revenue_audit_sink] = lambda: failing_sink

    with (
        TestClient(app) as client,
        pytest.raises(RuntimeError, match="staged third-append failure"),
    ):
        client.get(
            "/revenue/months/2026-03/gap-explanation",
            headers=auth_headers("finance_viewer", "global"),
        )

    # The boundary retracted the accepted prefix: not one record retained.
    assert failing_sink.records == []


def test_locked_month_reads_pass_through_close_status(tmp_path):
    """The read path is never close-guarded; LOCKED is surfaced read-only."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/gap-explanation",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    assert response.json()["close_status"] == "LOCKED"


def test_gap_explanation_rejects_non_usd_currency(tmp_path):
    """The shared USD-only gate rejects other currencies with the shared 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/gap-explanation?currency=EUR",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "currency must be USD until exchange-rate support is implemented"
    )


def test_assistant_cannot_read_gap_explanation(tmp_path):
    """A role without finance visibility fails the first gate."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/gap-explanation",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_month_scoped_finance_viewer_still_needs_global_revenue_gate(tmp_path):
    """The composed read is stricter than the bank read alone.

    A finance-month-scoped viewer can read bank reconciliation for their
    month, but this endpoint discloses revenue-leg numbers too, so the
    global VIEW_REVENUE gate must still refuse them.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/gap-explanation",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def _principal_with_grants(*grants: PermissionGrant) -> UserPrincipal:
    """Build a principal holding exactly the given direct permission grants."""
    return UserPrincipal(
        user_id=str(USER_ID),
        email="gap-explanation@example.com",
        direct_permissions=grants,
    )


def _unreached_repositories() -> dict[str, object]:
    """Dependencies for direct calls that must fail before any data access.

    The platform session is an _UnreachedRepository on purpose: the denial
    must precede the composed-read snapshot begin as well, so a reordering
    that starts the transaction before the permission gates trips these
    tests the same way touching a repository would.
    """
    return {
        "revenue_repository": _UnreachedRepository(),
        "payment_repository": _UnreachedRepository(),
        "bank_repository": _UnreachedRepository(),
        "close_repository": _UnreachedRepository(),
        "platform_session": _UnreachedRepository(),
        "audit_sink": InMemoryAuditSink(),
    }


def test_second_gate_requires_confidence_view():
    """VIEW_REVENUE alone is refused at the confidence gate.

    The response carries confidence labels/scores on every component and
    residual, so the smart-alerts confidence gate applies. No seeded role
    splits these permissions, so the gate is isolated with direct grants.
    """
    user = _principal_with_grants(
        PermissionGrant(
            permission=Permission.VIEW_REVENUE,
            scope=AccessScope.global_scope(),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        get_month_gap_explanation(
            month="2026-03",
            user=user,
            currency="USD",
            **_unreached_repositories(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == ("Missing permission: analytics.view_confidence")


def test_third_gate_requires_finalized_payments_view():
    """Revenue + confidence together still stop at the finalized-payments gate."""
    user = _principal_with_grants(
        PermissionGrant(
            permission=Permission.VIEW_REVENUE,
            scope=AccessScope.global_scope(),
        ),
        PermissionGrant(
            permission=Permission.VIEW_CONFIDENCE,
            scope=AccessScope.global_scope(),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        get_month_gap_explanation(
            month="2026-03",
            user=user,
            currency="USD",
            **_unreached_repositories(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == ("Missing permission: finance.view_finalized_payments")


def test_fourth_gate_requires_bank_reconciliation_view():
    """The first three grants together still stop at the bank gate."""
    user = _principal_with_grants(
        PermissionGrant(
            permission=Permission.VIEW_REVENUE,
            scope=AccessScope.global_scope(),
        ),
        PermissionGrant(
            permission=Permission.VIEW_CONFIDENCE,
            scope=AccessScope.global_scope(),
        ),
        PermissionGrant(
            permission=Permission.VIEW_FINALIZED_PAYMENTS,
            scope=AccessScope.finance_month("2026-03"),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        get_month_gap_explanation(
            month="2026-03",
            user=user,
            currency="USD",
            **_unreached_repositories(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == ("Missing permission: finance.view_bank_reconciliation")


class _UnreachedRepository:
    """Stand-in dependency that fails the test if any data access happens."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"data access via {name!r} before permission gates")
