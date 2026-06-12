"""Unit tests for the in-process ConnectorJobExecutor worker + registry."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.errors import OAuthRefreshError
from ums_smart_revenue.connectors.runs.executor import (
    ConnectorJobActor,
    ConnectorJobExecutor,
)
from ums_smart_revenue.connectors.runs.orchestrator import ConnectorRunOutcome
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import (
    AuditLogORM,
    SecurityBase,
    UserORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant
from ums_smart_revenue.tenancy.models import TenantStatus

TENANT = UUID(UMS_TENANT_ID)
ACTOR = ConnectorJobActor(user_id=str(uuid4()), email="ops@example.com")


def _factory(tmp_path) -> sessionmaker:
    url = f"sqlite+pysqlite:///{(tmp_path / 'exec.db').as_posix()}"
    engine = create_engine(url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        # The worker enters connector_tenant_context(session=...), which loads
        # the tenant by id and enforces the ACTIVE-only gate; seed an ACTIVE
        # tenant for UMS_TENANT_ID so the lifecycle check passes.
        session.add(
            TenantORM(
                id=TENANT,
                slug="ums-test",
                display_name="UMS Test",
                primary_currency="USD",
                status=TenantStatus.ACTIVE,
                onboarding_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UserORM(
                id=UUID(ACTOR.user_id),
                email=ACTOR.email,
                display_name="Ops",
            )
        )
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _outcome() -> ConnectorRunOutcome:
    return ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])


def test_run_job_uses_own_session_and_sets_tenant_context(tmp_path) -> None:
    """The worker opens its own session and TENANT_CTX is set inside run_one."""
    factory = _factory(tmp_path)
    seen: dict[str, object] = {}

    def _fake_run_one(session, **kwargs):
        tenant = get_current_tenant()
        seen["tenant_id"] = None if tenant is None else tenant.id
        seen["session_is_factory"] = isinstance(session, Session)
        return _outcome()

    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _fake_run_one
        ):
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
    finally:
        executor.close()

    assert seen["tenant_id"] == TENANT
    assert seen["session_is_factory"] is True
    # TENANT_CTX is reset after the worker exits (no leak into this thread).
    assert get_current_tenant() is None


def test_run_job_removes_registry_entry_on_success(tmp_path) -> None:
    """A successful run clears its registry key in finally."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()


def test_run_job_bucket_a_failure_writes_audit_and_does_not_propagate(
    tmp_path,
) -> None:
    """A Bucket-A GoogleConnectorError is caught, audited, never re-raised."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            executor._register(key)
            # Must NOT raise out of the worker body.
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    assert row.details["error_class"] == "OAuthRefreshError"
    # Canned class name only — never the exception text.
    assert "revoked" not in str(row.details)


def test_run_job_unexpected_exception_swallowed_and_registry_cleared(
    tmp_path,
) -> None:
    """A projection-style re-raise is swallowed; the registry key is cleared."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        raise RuntimeError("projection failed; run already FAILED+audited")

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    # An unexpected (non-Bucket-A) error logs but writes NO job_failed audit.
    with factory() as session:
        assert session.scalars(select(AuditLogORM)).all() == []


def test_submit_then_future_result_clears_active_flag(tmp_path) -> None:
    """submit() registers the key, runs the worker, and clears it on completion."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            future = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            future.result(timeout=10)
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# submit_if_absent + activate + cancel_reservation: the atomic dedup flow
# --------------------------------------------------------------------------


def test_submit_if_absent_returns_none_for_duplicate(tmp_path) -> None:
    """A second submit_if_absent for the same scope returns None (atomic guard)."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        first = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=ACTOR,
        )
        assert first is not None
        second = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=ACTOR,
        )
        assert second is None
        # has_active_job still reports True for the in-flight reservation.
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is True
    finally:
        executor.close()


def test_activate_enqueues_worker_and_replaces_reservation(tmp_path) -> None:
    """activate() turns a reservation into a real Future and runs the worker."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            reservation = executor.submit_if_absent(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            assert reservation is not None
            future = executor.activate(reservation)
            future.result(timeout=10)
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()


def test_activate_is_idempotent_for_same_reservation(tmp_path) -> None:
    """Calling activate() twice on the same reservation returns the same Future."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            reservation = executor.submit_if_absent(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            assert reservation is not None
            first = executor.activate(reservation)
            second = executor.activate(reservation)
            assert first is second
            first.result(timeout=10)
    finally:
        executor.close()


def test_cancel_reservation_drops_in_flight_slot(tmp_path) -> None:
    """cancel_reservation removes a pending reservation; subsequent submit_if_absent succeeds."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        reservation = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=ACTOR,
        )
        assert reservation is not None
        assert executor.cancel_reservation(reservation) is True
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
        # A new submit_if_absent succeeds and returns a fresh reservation.
        second = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=ACTOR,
        )
        assert second is not None
        executor.cancel_reservation(second)
    finally:
        executor.close()


def test_cancel_reservation_returns_false_when_already_activated(tmp_path) -> None:
    """cancel_reservation is a no-op (returns False) if the slot is already a Future."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            reservation = executor.submit_if_absent(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            assert reservation is not None
            future = executor.activate(reservation)
            # The future is now in the registry; cancel_reservation must NOT
            # remove it (would kill the in-flight worker).
            assert executor.cancel_reservation(reservation) is False
            future.result(timeout=10)
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# Dry-run outcome audit + service-principal pre-start failure audit
# --------------------------------------------------------------------------


def test_run_job_dry_run_writes_completed_audit_and_clears_registry(
    tmp_path,
) -> None:
    """A dry-run worker audits one job_dry_run_completed row with counts + failures."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    dry_outcome = ConnectorRunOutcome(
        run=None,
        counts={
            "reports_attempted": 2,
            "reports_succeeded": 1,
            "reports_failed": 1,
            "rows_upserted_total": 5,
        },
        per_report_failures=[("report-type-2", "ParserError")],
    )

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: dry_outcome,
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=True,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_dry_run_completed"
    assert row.details["dry_run"] is True
    assert row.details["counts"] == dry_outcome.counts
    assert row.details["per_report_failures"] == [
        {"report_type": "report-type-2", "error_class": "ParserError"}
    ]


def test_run_job_service_principal_failure_writes_bucket_a_audit(
    tmp_path,
) -> None:
    """A pre-start ConnectorServicePrincipalUnavailableError is audited (no ValueError swallow)."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    from ums_smart_revenue.connectors.google.errors import (
        ConnectorServicePrincipalUnavailableError,
    )

    def _boom(session, **kwargs):
        raise ConnectorServicePrincipalUnavailableError(
            env_var="UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"
        )

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    # Canned class name only -- the env var is in the reason, not the message.
    assert row.details["error_class"] == "ConnectorServicePrincipalUnavailableError"
