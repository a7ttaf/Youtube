# ============================================================================
# Purpose: Unit tests for ConnectorJobExecutor worker + registry semantics.
# Database/ORM: In-memory SQLite session factories for executor workers.
# Standards: Fail-closed worker boundaries; no suppressions.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> subject.
# ============================================================================
"""Unit tests for the in-process ConnectorJobExecutor worker + registry."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

import ums_smart_revenue.connectors.runs.executor as executor_module
from ums_smart_revenue.config.logging_config import configure_logging, restore_logging
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
    """factory."""
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
    """outcome."""
    return ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])


def test_run_job_uses_own_session_and_sets_tenant_context(tmp_path) -> None:
    """The worker opens its own session and TENANT_CTX is set inside run_one."""
    factory = _factory(tmp_path)
    seen: dict[str, object] = {}

    def _fake_run_one(session, **kwargs):
        """fake run one."""
        tenant = get_current_tenant()
        seen["tenant_id"] = None if tenant is None else tenant.id
        seen["session_is_factory"] = isinstance(session, Session)
        return _outcome()

    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _fake_run_one):
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
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()


def test_run_job_bucket_a_failure_writes_audit_and_does_not_propagate(tmp_path, caplog) -> None:
    """A Bucket-A GoogleConnectorError is caught, audited, never re-raised."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        """boom."""
        raise OAuthRefreshError(
            inner=RuntimeError(
                "https://user:password@example.test/token?X-Goog-Signature=signed-secret"
            )
        )

    try:
        with (
            patch("ums_smart_revenue.connectors.runs.executor.run_one", _boom),
            caplog.at_level("ERROR", logger="ums_smart_revenue.connectors.runs.executor"),
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    assert row.details["error_class"] == "OAuthRefreshError"
    # Canned class name only — never the exception text.
    assert "signed-secret" not in str(row.details)
    expected = [
        record for record in caplog.records if "Connector job failed" in record.getMessage()
    ]
    assert len(expected) == 1
    assert expected[0].exc_info is None
    assert "failure_category=google_connector_failure" in expected[0].getMessage()
    assert "signed-secret" not in expected[0].getMessage()


def test_run_job_unexpected_exception_swallowed_and_registry_cleared(
    tmp_path,
) -> None:
    """A projection-style re-raise is swallowed; the registry key is cleared."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        """boom."""
        raise RuntimeError("projection failed; run already FAILED+audited")

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _boom):
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()


def test_run_job_unexpected_exception_redacts_guarded_owner_traceback(tmp_path) -> None:
    """The real unexpected-worker logger cannot publish CMS owner URL forms."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    stream = io.StringIO()
    logging_configuration = configure_logging(level="ERROR", stream=stream)
    owner_id = "GuardedOwnerUnexpectedWorker123"

    def _boom(_session, **_kwargs):
        raise RuntimeError(
            "https://youtube.test/reports?"
            f"onBehalfOfContentOwner={owner_id}&"
            f"ids=contentOwner%3D%3D{owner_id}&safe=kept"
        )

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _boom):
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
        restore_logging(logging_configuration)

    output = stream.getvalue()
    assert owner_id not in output
    assert "onBehalfOfContentOwner=[REDACTED]" in output
    assert "ids=contentOwner%3D%3D[REDACTED]" in output
    assert "safe=kept" in output

    # An unexpected (non-Bucket-A) error logs but writes NO job_failed audit.
    with factory() as session:
        assert session.scalars(select(AuditLogORM)).all() == []


def test_submit_then_future_result_clears_active_flag(tmp_path) -> None:
    """submit() registers the key, runs the worker, and clears it on completion."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# submit_if_absent + activate + cancel_reservation: the atomic dedup flow
# --------------------------------------------------------------------------


def test_submit_if_absent_returns_none_for_duplicate(tmp_path) -> None:
    """A second submit_if_absent for the same scope returns None (atomic guard)."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is True
        )
    finally:
        executor.close()


def test_activate_enqueues_worker_and_replaces_reservation(tmp_path) -> None:
    """activate() turns a reservation into a real Future and runs the worker."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()


def test_activate_is_idempotent_for_same_reservation(tmp_path) -> None:
    """Calling activate() twice on the same reservation returns the same Future."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
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
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
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


def test_run_job_dry_run_no_failures_writes_empty_per_report_failures(
    tmp_path,
) -> None:
    """A dry-run with no per-report failures still audits the completed row.

    The audited row carries an empty per_report_failures list.
    """
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    clean_outcome = ConnectorRunOutcome(
        run=None,
        counts={
            "reports_attempted": 2,
            "reports_succeeded": 2,
            "reports_failed": 0,
            "rows_upserted_total": 8,
        },
        per_report_failures=[],
    )

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: clean_outcome,
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
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_dry_run_completed"
    assert row.details["per_report_failures"] == []


def test_run_job_service_principal_failure_writes_bucket_a_audit(
    tmp_path,
) -> None:
    """A pre-start ConnectorServicePrincipalUnavailableError is audited (no ValueError swallow)."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    from ums_smart_revenue.connectors.google.errors import (
        ConnectorServicePrincipalUnavailableError,
    )

    def _boom(session, **kwargs):
        """boom."""
        raise ConnectorServicePrincipalUnavailableError(
            env_var="UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"
        )

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _boom):
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
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
            )
            is False
        )
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    # Canned class name only -- the env var is in the reason, not the message.
    assert row.details["error_class"] == "ConnectorServicePrincipalUnavailableError"


def test_run_job_inactive_tenant_failure_writes_audit(tmp_path) -> None:
    """A pre-start TenantLifecycleError for an inactive tenant is still audited.

    ``_audit_failed_before_start`` must not re-enter ``connector_tenant_context``,
    because an inactive/suspended tenant would raise the same lifecycle error
    again and prevent the ``job_failed_before_start`` row from being written.
    """
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)

    with factory() as session:
        tenant = session.get(TenantORM, TENANT)
        tenant.status = TenantStatus.SUSPENDED
        session.commit()

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            side_effect=AssertionError("run_one should not be called for inactive tenant"),
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

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    assert row.details["error_class"] == "TenantLifecycleError"
    assert row.details["report_month"] == "2026-03"


def test_close_audits_queued_jobs_cancelled_by_shutdown(tmp_path) -> None:
    """A deterministic close() audits accepted jobs that never started.

    With max_workers=1, a second submitted job sits in the ThreadPoolExecutor
    work queue. If the app shuts down before that worker starts,
    cancel_futures=True drops the future and ``_run_job`` never runs, so
    nothing would write a failure audit. close() now cancels futures first,
    then audits any future that is ``cancelled()`` as a
    ``job_failed_before_start`` row with ``error_class="ExecutorShutdown"``.
    Running futures are not audited as pre-start failures.
    """
    import threading
    import time

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    started = threading.Event()

    def _slow_run_one(session, **kwargs):
        """slow run one."""
        started.set()
        time.sleep(0.5)
        return _outcome()

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _slow_run_one):
            first = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            second = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-04",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            # Wait until the first worker has actually started; only then is
            # the second future queued behind it.
            started.wait(timeout=5)
            # The second future is still pending in the queue.
            executor.close()
            # close() waits for the running first job; the queued second is cancelled.
            _ = first
            _ = second
    finally:
        # If close() already ran, calling it again is a no-op. If an
        # exception was raised mid-test, ensure we still clean up.
        executor.close()

    # The queued second job is the only shutdown-audited entry; the running
    # first job must not be misclassified as a pre-start failure.
    with factory() as session:
        audits = session.scalars(select(AuditLogORM)).all()
    shutdown_audits = [
        a
        for a in audits
        if a.details.get("action") == "job_failed_before_start"
        and a.details.get("error_class") == "ExecutorShutdown"
    ]
    assert len(shutdown_audits) == 1
    assert shutdown_audits[0].event_type == "CONNECTOR_JOB_RUN"
    assert shutdown_audits[0].entity_id == "youtube_reporting:acct-1"
    assert shutdown_audits[0].details["report_month"] == "2026-04"
    # No false pre-start audit for the job that was already running.
    assert not any(a.details.get("report_month") == "2026-03" for a in shutdown_audits)
    # The registry is cleared after shutdown.
    assert executor._registry == {}


def test_close_retains_hung_worker_until_it_really_settles(tmp_path, monkeypatch) -> None:
    """Repeated close calls stay false while the same retained worker hangs."""
    import threading
    import time

    import ums_smart_revenue.connectors.runs.executor as executor_module

    monkeypatch.setattr(executor_module, "CLOSE_DRAIN_TIMEOUT_SECONDS", 0.2)

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    started = threading.Event()
    release = threading.Event()

    def _hang_forever(session, **kwargs):
        """hang forever."""
        started.set()
        release.wait(timeout=30)
        return _outcome()

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _hang_forever):
            future = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-hang",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            assert started.wait(timeout=5)
            began = time.monotonic()
            assert executor.close() is False
            assert executor.close() is False
            elapsed = time.monotonic() - began
            # FIX: The dedup registry is cleared at shutdown, but the worker
            # must remain separately reachable until it actually settles.
            assert executor._registry == {}
            assert future in executor._shutdown_pending_futures
        assert elapsed < 2.0, f"close() hung for {elapsed:.2f}s"
        release.set()
        future.result(timeout=5)
        assert executor.close() is True
    finally:
        release.set()
        executor.wait_for_shutdown_completion()


def test_wait_for_shutdown_completion_retains_timed_out_audit_thread(tmp_path, monkeypatch) -> None:
    """The unbounded waiter keeps a timed-out shutdown audit reachable."""
    import threading
    import time

    import ums_smart_revenue.connectors.runs.executor as executor_module

    monkeypatch.setattr(executor_module, "SHUTDOWN_AUDIT_BUDGET_SECONDS", 0.05)

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    audit_started = threading.Event()
    audit_release = threading.Event()
    waiter_finished = threading.Event()

    def _blocked_audit(cancelled) -> bool:
        """Hold the synthetic audit beyond close's bounded audit budget."""
        _ = cancelled
        audit_started.set()
        audit_release.wait(timeout=30)
        return True

    job_key = (TENANT, "youtube_reporting", "acct-audit", "2026-03")
    try:
        with patch.object(executor, "_write_shutdown_audits", _blocked_audit):
            deadline = time.monotonic() + 1.0
            assert (
                executor._audit_cancelled_within_budget(
                    [(job_key, ACTOR, uuid4())], deadline=deadline
                )
                is False
            )
            assert audit_started.is_set()

            waiter = threading.Thread(
                target=lambda: (
                    executor.wait_for_shutdown_completion(),
                    waiter_finished.set(),
                )
            )
            waiter.start()
            assert not waiter_finished.wait(timeout=0.1)
            audit_release.set()
            assert waiter_finished.wait(timeout=5)
            waiter.join(timeout=5)

        assert executor.close() is True
        executor.wait_for_shutdown_completion()
    finally:
        audit_release.set()
        executor.wait_for_shutdown_completion()


def test_close_logs_worker_exception_from_done_futures(tmp_path, caplog) -> None:
    """Worker exceptions observed during close drain must be logged, not swallowed."""
    import logging
    from concurrent.futures import Future

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    boom = Future()
    boom.set_exception(RuntimeError("drain boom"))

    with (
        patch.object(
            executor,
            "_audit_pending_on_shutdown",
            return_value=([boom], True),
        ),
        caplog.at_level(logging.ERROR),
    ):
        executor.close()

    matching = [
        record
        for record in caplog.records
        if "Connector job worker raised during executor close drain" in record.getMessage()
    ]
    assert matching, caplog.text
    assert any(record.exc_info is not None for record in matching)


def test_close_reports_unclean_while_immediate_shutdown_audit_is_outstanding(
    tmp_path,
) -> None:
    """A timed-out audit cannot be reported as a fully clean close."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)

    with patch.object(
        executor,
        "_audit_pending_on_shutdown",
        return_value=([], False),
    ):
        assert executor.close() is False


def test_abandoned_durable_intent_recovers_exactly_once(tmp_path) -> None:
    """Startup reconciliation closes a committed, never-dispatched intent once."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    reservation = executor.submit_if_absent(
        tenant_id=TENANT,
        connector_key="youtube_reporting",
        account_id="acct-recovery",
        report_month="2026-03",
        dry_run=False,
        triggered_by_user_id=UUID(ACTOR.user_id),
        actor_identity=ACTOR,
    )
    assert reservation is not None
    with factory() as session:
        executor.persist_submission_intent(
            session=session,
            reservation=reservation,
            reason="recovery test submission",
        )
        session.commit()
    assert executor.cancel_reservation(reservation) is True

    assert executor.recover_abandoned_submission_intents() == 1
    assert executor.recover_abandoned_submission_intents() == 0
    executor.close()

    with factory() as session:
        rows = session.scalars(
            select(AuditLogORM).where(AuditLogORM.request_id == str(reservation.job_id))
        ).all()
    actions = [row.details["action"] for row in rows]
    assert len(actions) == 2
    assert set(actions) == {"job_submitted", "job_failed_before_start"}
    failure = next(row for row in rows if row.details["action"] == "job_failed_before_start")
    assert failure.details["error_class"] == "ExecutorShutdownRecovery"


def test_abandoned_intent_recovery_is_batched_server_side(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery uses bounded NOT EXISTS queries instead of a historical IN list."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    monkeypatch.setattr(executor_module, "_JOB_RECOVERY_BATCH_SIZE", 2)
    reservations = []
    for index in range(5):
        reservation = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id=f"acct-recovery-batch-{index}",
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=UUID(ACTOR.user_id),
            actor_identity=ACTOR,
        )
        assert reservation is not None
        reservations.append(reservation)
        with factory() as session:
            executor.persist_submission_intent(
                session=session,
                reservation=reservation,
                reason="batched recovery test submission",
            )
            session.commit()
            if index >= 3:
                assert executor._record_dispatch_started(
                    session=session,
                    tenant_id=reservation.tenant_id,
                    connector_key=reservation.connector_key,
                    account_id=reservation.account_id,
                    report_month=reservation.report_month,
                    actor_identity=reservation.actor_identity,
                    job_id=reservation.job_id,
                )
                session.commit()
        assert executor.cancel_reservation(reservation) is True

    statements: list[str] = []
    engine = factory.kw["bind"]

    def _capture_sql(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if "audit_logs" in statement.lower():
            statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", _capture_sql)
    try:
        assert executor.recover_abandoned_submission_intents() == 3
        assert executor.recover_abandoned_submission_intents() == 0
    finally:
        event.remove(engine, "before_cursor_execute", _capture_sql)
        executor.close()

    recovery_selects = [
        statement
        for statement in statements
        if statement.startswith("select")
        and "connector_job_intent" in statement
        and "not (exists" in statement
    ]
    assert len(recovery_selects) >= 3
    assert all(" limit ? offset ?" in statement for statement in recovery_selects)
    assert all(
        "connector_job_intent.request_id in" not in statement for statement in recovery_selects
    )

    with factory() as session:
        lifecycle = list(
            session.scalars(
                select(AuditLogORM).where(AuditLogORM.event_type == "CONNECTOR_JOB_RUN")
            ).all()
        )
    failures = [row for row in lifecycle if row.details["action"] == "job_failed_before_start"]
    dispatches = [row for row in lifecycle if row.details["action"] == "job_dispatch_started"]
    assert len(failures) == 3
    assert len(dispatches) == 2


def test_abandoned_duplicate_submission_intents_recover_one_logical_job(tmp_path) -> None:
    """Historical duplicate submission rows produce one terminal audit edge."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    reservation = executor.submit_if_absent(
        tenant_id=TENANT,
        connector_key="youtube_reporting",
        account_id="acct-duplicate-intent",
        report_month="2026-03",
        dry_run=False,
        triggered_by_user_id=UUID(ACTOR.user_id),
        actor_identity=ACTOR,
    )
    assert reservation is not None
    with factory() as session:
        for reason in ("first historical intent", "duplicate historical intent"):
            executor.persist_submission_intent(
                session=session,
                reservation=reservation,
                reason=reason,
            )
        session.commit()
    assert executor.cancel_reservation(reservation) is True

    try:
        assert executor.recover_abandoned_submission_intents() == 1
        assert executor.recover_abandoned_submission_intents() == 0
    finally:
        executor.close()

    with factory() as session:
        rows = session.scalars(
            select(AuditLogORM).where(AuditLogORM.request_id == str(reservation.job_id))
        ).all()
    actions = [str(row.details["action"]) for row in rows]
    assert actions.count("job_submitted") == 2
    assert actions.count("job_failed_before_start") == 1


def test_dispatch_started_edge_prevents_false_shutdown_recovery(tmp_path) -> None:
    """A committed dispatch edge proves the job was not cancelled while queued."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    reservation = executor.submit_if_absent(
        tenant_id=TENANT,
        connector_key="youtube_reporting",
        account_id="acct-started",
        report_month="2026-04",
        dry_run=False,
        triggered_by_user_id=UUID(ACTOR.user_id),
        actor_identity=ACTOR,
    )
    assert reservation is not None
    with factory() as session:
        executor.persist_submission_intent(
            session=session,
            reservation=reservation,
            reason="started recovery test submission",
        )
        session.commit()
        executor._record_dispatch_started(
            session=session,
            tenant_id=reservation.tenant_id,
            connector_key=reservation.connector_key,
            account_id=reservation.account_id,
            report_month=reservation.report_month,
            actor_identity=reservation.actor_identity,
            job_id=reservation.job_id,
        )
        session.commit()
    assert executor.cancel_reservation(reservation) is True

    assert executor.recover_abandoned_submission_intents() == 0
    executor.close()

    with factory() as session:
        rows = session.scalars(
            select(AuditLogORM).where(AuditLogORM.request_id == str(reservation.job_id))
        ).all()
    actions = [row.details["action"] for row in rows]
    assert len(actions) == 2
    assert set(actions) == {"job_submitted", "job_dispatch_started"}
