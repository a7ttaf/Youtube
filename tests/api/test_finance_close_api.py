from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.finance.month_close import FinanceMonthCloseEntry
from ums_smart_revenue.finance.month_close_readiness import (
    FinanceCloseReadiness,
    SqlAlchemyFinanceCloseReadinessService,
)

USER_ID = UUID("00000000-0000-0000-0000-000000005001")


def auth_headers(
    role: str, scope_type: str = "finance-month", scope_id: str = "2026-03"
) -> dict[str, str]:
    """Build trusted-gateway headers for finance-close API tests."""
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": "finance-close@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-scope-id": scope_id,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    """Return an isolated SQLite URL for a finance-close test database."""
    return f"sqlite+pysqlite:///{(tmp_path / 'finance-close.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Create finance-close test tables and the acting user row."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="finance-close@example.com",
                display_name="Finance Close User",
            )
        )
        session.commit()


def test_finance_close_entry_to_api_copies_allocation_rule_payload():
    """Serializer copies allocation-rule payloads instead of sharing dicts."""
    payload = {"basis": "gross_revenue_usd"}
    entry = FinanceMonthCloseEntry(
        month="2026-03",
        status="OPEN",
        allocation_method="gross_revenue_proportional",
        allocation_rule_payload=payload,
        locked_by=None,
        locked_at=None,
        unlocked_by=None,
        unlocked_at=None,
    )

    api_payload = entry.to_api()
    api_payload["allocation_rule_payload"]["basis"] = "mutated"

    assert entry.allocation_rule_payload == {"basis": "gross_revenue_usd"}
    assert payload == {"basis": "gross_revenue_usd"}


def test_finance_admin_can_lock_month_with_audit(tmp_path):
    """Finance Admin can lock an open month and emit a lock audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Final payment reconciliation completed"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["month"] == "2026-03"
    assert response.json()["status"] == "LOCKED"
    assert close.locked_by == USER_ID
    assert audit_log.event_type == "MONTH_LOCKED"
    assert audit_log.reason == "Final payment reconciliation completed"


def test_finance_close_rejects_blank_lock_reason_before_state_change(tmp_path):
    """Whitespace reasons are rejected before close-state mutation or audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "   "},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, must not be blank"
    assert close is None
    assert audit_logs == []


def test_finance_close_rejects_malformed_actor_id_before_state_change(tmp_path):
    """Malformed actor ids return auth/input errors instead of close conflicts."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin")
    headers["x-user-id"] = "not-a-uuid"

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=headers,
        json={"reason": "Reject malformed actor id"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_logs = session.scalars(select(AuditLogORM)).all()

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid request"
    assert close is None
    assert audit_logs == []


def test_finance_close_rejects_invalid_month_path(tmp_path):
    """Invalid month path values are rejected before close-state lookup."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/finance-close/2026-19",
        headers=auth_headers("finance_admin", scope_id="2026-19"),
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "month must use YYYY-MM with a calendar month from 01 to 12"
    )


def test_get_finance_close_does_not_create_missing_month(tmp_path):
    """Reading a missing close row returns 404 without creating state."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/finance-close/2026-03", headers=auth_headers("finance_admin")
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")

    assert response.status_code == 404
    assert response.json()["detail"] == "Finance month close record not found"
    assert close is None


def test_finance_close_read_records_audit_event(tmp_path):
    """Reading finance-close state records sensitive access without mutating state."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID))
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/finance-close/2026-03", headers=auth_headers("finance_admin")
    )

    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["status"] == "LOCKED"
    assert audit_log.event_type == "MONTH_CLOSE_VIEWED"
    assert audit_log.sensitive is True
    assert audit_log.details["read_type"] == "summary"


def test_finance_admin_cannot_lock_already_locked_month(tmp_path):
    """Locking an already locked month returns the close-state conflict."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID)
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should be rejected"},
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Finance month cannot be locked from its current state"
    )


def test_finance_admin_cannot_lock_month_with_pending_manual_override(tmp_path):
    """Pending manual overrides block month locking with blocker details."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            RevenueManualOverrideORM(
                id=uuid4(),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                adjustment_revenue_usd=Decimal("125.50"),
                reason="Pending source correction",
                created_by=USER_ID,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should wait for approval"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "Finance month has unresolved close blockers",
        "blockers": [
            {
                "blocker_type": "PENDING_MANUAL_OVERRIDES",
                "severity": "HIGH",
                "count": 1,
                "message": (
                    "1 pending manual override requires approval before locking "
                    "2026-03."
                ),
            }
        ],
    }


def test_finance_close_readiness_reports_reconciliation_variance(tmp_path):
    """Readiness and lock responses include reconciliation issue blockers."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("1000.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("930.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    readiness = client.get(
        "/finance-close/2026-03/readiness", headers=auth_headers("finance_admin")
    )
    lock_response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should wait for reconciliation"},
    )

    assert readiness.status_code == 200
    assert readiness.json() == {
        "month": "2026-03",
        "ready": False,
        "blockers": [
            {
                "blocker_type": "RECONCILIATION_ISSUES",
                "severity": "HIGH",
                "count": 1,
                "message": (
                    "1 channel has unresolved reconciliation issues for 2026-03."
                ),
            }
        ],
    }
    assert lock_response.status_code == 409
    lock_detail = lock_response.json()["detail"]
    assert lock_detail["message"] == "Finance month has unresolved close blockers"
    assert lock_detail["blockers"] == [
        {
            "blocker_type": "RECONCILIATION_ISSUES",
            "severity": "HIGH",
            "count": 1,
            "message": ("1 channel has unresolved reconciliation issues for 2026-03."),
        }
    ]


def test_finance_close_readiness_blocks_missing_required_revenue_facts(tmp_path):
    """Required active channels without facts block readiness and locking."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                youtube_channel_id="channel-missing-revenue",
                channel_name="Missing Revenue Channel",
                revenue_required=True,
                active=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    readiness = client.get(
        "/finance-close/2026-03/readiness",
        headers=auth_headers("finance_admin"),
    )
    lock_response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should wait for missing required revenue"},
    )

    expected_blockers = [
        {
            "blocker_type": "MISSING_REVENUE_FACTS",
            "severity": "HIGH",
            "count": 1,
            "message": ("1 revenue-required channel has no revenue facts for 2026-03."),
        }
    ]
    assert readiness.status_code == 200
    assert readiness.json() == {
        "month": "2026-03",
        "ready": False,
        "blockers": expected_blockers,
    }
    assert lock_response.status_code == 409
    assert lock_response.json()["detail"] == {
        "message": "Finance month has unresolved close blockers",
        "blockers": expected_blockers,
    }


def test_finance_close_readiness_counts_bulk_missing_required_revenue_facts(tmp_path):
    """Readiness reports the full count of missing required channel facts."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            YouTubeChannelORM(
                id=uuid4(),
                youtube_channel_id=f"channel-missing-revenue-{index:02d}",
                channel_name=f"Missing Revenue Channel {index:02d}",
                revenue_required=True,
                active=True,
            )
            for index in range(12)
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/finance-close/2026-03/readiness",
        headers=auth_headers("finance_admin"),
    )

    assert response.status_code == 200
    assert response.json()["blockers"] == [
        {
            "blocker_type": "MISSING_REVENUE_FACTS",
            "severity": "HIGH",
            "count": 12,
            "message": (
                "12 revenue-required channels have no revenue facts for 2026-03."
            ),
        }
    ]


def test_finance_lock_rechecks_after_channel_becomes_revenue_required(tmp_path):
    """Locking rechecks current channel state after a stale readiness view."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                youtube_channel_id="channel-stale-readiness",
                channel_name="Stale Readiness Channel",
                revenue_required=False,
                revenue_source_status="PERFORMANCE_ONLY",
                active=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    stale_readiness = client.get(
        "/finance-close/2026-03/readiness",
        headers=auth_headers("finance_admin"),
    )
    with Session(engine) as session:
        channel = session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == "channel-stale-readiness"
            )
        ).one()
        channel.revenue_required = True
        channel.revenue_source_status = "MISSING_REVENUE_SOURCE"
        session.commit()

    lock_response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Should recheck current channel state"},
    )

    assert stale_readiness.status_code == 200
    assert stale_readiness.json() == {"month": "2026-03", "ready": True, "blockers": []}
    assert lock_response.status_code == 409
    assert lock_response.json()["detail"]["blockers"] == [
        {
            "blocker_type": "MISSING_REVENUE_FACTS",
            "severity": "HIGH",
            "count": 1,
            "message": "1 revenue-required channel has no revenue facts for 2026-03.",
        }
    ]


def test_finance_lock_requests_pessimistic_readiness_recheck(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Locking asks readiness to use row-lock mode before state transition."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    observed_for_update: list[bool] = []

    def recording_check_month(
        self: SqlAlchemyFinanceCloseReadinessService,
        month: str,
        *,
        for_update: bool = False,
    ) -> FinanceCloseReadiness:
        """Capture whether the lock path requested row-lock mode."""
        del self
        observed_for_update.append(for_update)
        return FinanceCloseReadiness(month=month, blockers=[])

    monkeypatch.setattr(
        SqlAlchemyFinanceCloseReadinessService,
        "check_month",
        recording_check_month,
    )
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_admin"),
        json={"reason": "Verify lock-time readiness query locks rows"},
    )

    assert response.status_code == 200
    assert observed_for_update == [True]


def test_finance_close_readiness_ignores_performance_only_channels(tmp_path):
    """Performance-only channels without revenue facts do not block close."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                youtube_channel_id="channel-performance-only",
                channel_name="Performance Only Channel",
                revenue_required=False,
                revenue_source_status="PERFORMANCE_ONLY",
                active=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    readiness = client.get(
        "/finance-close/2026-03/readiness",
        headers=auth_headers("finance_admin"),
    )

    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert readiness.status_code == 200
    assert readiness.json() == {"month": "2026-03", "ready": True, "blockers": []}
    assert audit_log.event_type == "MONTH_CLOSE_VIEWED"
    assert audit_log.sensitive is True
    assert audit_log.details["read_type"] == "readiness"


def test_finance_viewer_cannot_lock_month(tmp_path):
    """Finance Viewer lacks permission to lock a finance month."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/lock",
        headers=auth_headers("finance_viewer"),
        json={"reason": "Should be denied"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.lock_month"


def test_finance_approver_can_unlock_month_with_audit(tmp_path):
    """Finance Approver can unlock a locked month with an audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID)
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/unlock",
        headers=auth_headers("finance_approver"),
        json={"reason": "Bank correction arrived after lock"},
    )

    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["status"] == "OPEN"
    assert close.unlocked_by == USER_ID
    assert audit_log.event_type == "MONTH_UNLOCKED"


def test_finance_approver_cannot_unlock_open_month(tmp_path):
    """Unlocking an open month returns the close-state conflict."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/unlock",
        headers=auth_headers("finance_approver"),
        json={"reason": "Should be rejected"},
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Finance month cannot be unlocked from its current state"
    )


def test_export_operator_cannot_change_allocation_rule(tmp_path):
    """Export Operator cannot change finance allocation-rule metadata."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("export_operator"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee"},
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "Missing permission: finance.change_allocation_rule"
    )


def test_finance_admin_can_record_allocation_rule_metadata_with_audit(tmp_path):
    """Finance Admin can record allocation metadata with an audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("finance_admin"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee", "basis": "gross_revenue_usd"},
            "reason": "Allocate confirmed transfer fee by gross revenue",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["allocation_method"] == "gross_revenue_proportional"
    assert close.allocation_rule_payload == {
        "gap_type": "transfer_fee",
        "basis": "gross_revenue_usd",
    }
    assert audit_log.event_type == "ALLOCATION_RULE_CHANGED"
    assert audit_log.details["allocation_method"] == "gross_revenue_proportional"


def test_allocation_rule_allows_non_uuid_gateway_actor_with_audit(tmp_path):
    """Allocation writes do not persist actor UUIDs; audit stores raw gateway subject."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin")
    headers["x-user-id"] = "gateway-subject-1"

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=headers,
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"basis": "gross_revenue_usd"},
            "reason": "Allocate with gateway subject",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        close = session.get(FinanceMonthCloseORM, "2026-03")
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert close.allocation_method == "gross_revenue_proportional"
    assert audit_log.user_id is None
    assert audit_log.event_type == "ALLOCATION_RULE_CHANGED"
    assert audit_log.details["actor_user_id"] == "gateway-subject-1"


def test_finance_admin_cannot_change_allocation_rule_on_locked_month(tmp_path):
    """Locked months reject allocation-rule metadata changes."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID)
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/finance-close/2026-03/allocate",
        headers=auth_headers("finance_admin"),
        json={
            "allocation_method": "gross_revenue_proportional",
            "rule_payload": {"gap_type": "transfer_fee"},
            "reason": "Should be rejected while locked",
        },
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Finance month allocation rule cannot be changed from its current state"
    )
