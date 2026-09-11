# ============================================================================
# Purpose: Unit tests for the in-process ConnectorJobExecutor — worker session
#   isolation, registry dedup, reserve/activate/cancel, shutdown audits, and
#   the tracked audit-pool drain semantics.
# Database/ORM: Disposable file-backed SQLite via _factory; asserts audit_logs
#   rows and tenant-context behavior.
# Standards: Test-only; stubs run_one/_audit_failed_before_start where needed
#   and asserts typed failure audits instead of exception escapes.
# Blast Radius: None — test module.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> SUT.
# ============================================================================
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
    """Create a file-backed sessionmaker with the executor's schema seeded."""
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
    """Return the minimal ConnectorRunOutcome a stubbed run_one can return."""
    return ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])


def test_run_job_uses_own_session_and_sets_tenant_context(tmp_path) -> None:
    """The worker opens its own session and TENANT_CTX is set inside run_one."""
    factory = _factory(tmp_path)
    seen: dict[str, object] = {}

    def _fake_run_one(session, **kwargs):
        """Record the worker's session + tenant context for assertions."""
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


def test_run_job_bucket_a_failure_writes_audit_and_does_not_propagate(
    tmp_path,
) -> None:
    """A Bucket-A GoogleConnectorError is caught, audited, never re-raised."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        """Simulate a credential-refresh failure inside the worker."""
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _boom):
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
    assert "revoked" not in str(row.details)


def test_run_job_unexpected_exception_swallowed_and_registry_cleared(
    tmp_path,
) -> None:
    """A projection-style re-raise is swallowed; the registry key is cleared."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        """Simulate a projection failure after the run row was audited."""
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
        # Mirror the request lifecycle: a live reservation must be activated
        # or cancelled before close(), which now treats a dangling slot as an
        # in-flight post-commit hook and waits for it.
        executor.cancel_reservation(first)
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
    """A dry-run with no per-report failures still audits the completed row,
    with an empty per_report_failures list.
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
        """Simulate the service-actor gate raising before run_one starts."""
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
        """Hold the single worker slot so the second job stays queued."""
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
            # The first future may still complete; we do not wait for it.
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


def test_close_drains_activation_failure_audits_queued_mid_teardown(tmp_path) -> None:
    """A route-queued audit overlapping close() is accepted and drained.

    ``queue_failed_start_audit`` submits under ``_audit_lock`` while
    ``close()`` flips ``_audit_accepting`` under the same lock before calling
    ``shutdown(wait=True)``: a submission that entered first always lands in a
    live pool and is drained; one that arrives later is rejected cleanly
    (logged, never raised). Both interleavings are pinned deterministically.
    """
    import threading
    import time

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
    started = threading.Event()
    release = threading.Event()
    completed: list[str] = []

    def _slow_audit(**kwargs):
        """Block mid-write so close() provably overlaps the queued audit."""
        started.set()
        release.wait(timeout=5)
        completed.append(kwargs["error_class"])

    executor._audit_failed_before_start = _slow_audit  # stub out the DB write
    try:
        executor.queue_failed_start_audit(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
            error_class="RuntimeError",
            actor_identity=ACTOR,
        )
        assert started.wait(timeout=5)
        closer = threading.Thread(target=executor.close)
        closer.start()
        release.set()
        closer.join(timeout=10)
        assert not closer.is_alive(), "close() must drain the in-flight audit"
        assert completed == ["RuntimeError"]

        # A submission arriving after close() stops accepting is handed to
        # the last-chance daemon writer instead of being dropped — the audit
        # row still lands without blocking the caller.
        executor.queue_failed_start_audit(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-04",
            error_class="RuntimeError",
            actor_identity=ACTOR,
        )
        deadline = time.monotonic() + 5
        while completed != ["RuntimeError", "RuntimeError"]:
            assert time.monotonic() < deadline, "last-chance audit never ran"
            time.sleep(0.02)
    finally:
        release.set()
        executor.close()


def test_close_audits_committed_leftover_reservation_as_shutdown(tmp_path, monkeypatch) -> None:
    """A marked-committed reservation whose hook never ran is audited.

    The after_commit hook calls ``begin_post_commit`` before ``activate`` —
    the hook can only run post-commit, so a committed-marked reservation
    still in the registry at close() is an accepted job whose hook died
    mid-flight. close() must emit ``job_failed_before_start``
    (ExecutorShutdown) so the 202 keeps its lifecycle edge.
    """
    from ums_smart_revenue.connectors.runs import executor as executor_module

    monkeypatch.setattr(executor_module, "_PENDING_HOOK_GRACE_SECONDS", 0.05)
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
    # The hook entered (committed) but died before activate/end — the
    # reservation is a leftover close() must audit, not drop.
    executor.begin_post_commit(reservation)
    executor.close()

    with factory() as session:
        audits = session.scalars(select(AuditLogORM)).all()
    shutdown = [
        a
        for a in audits
        if a.details.get("action") == "job_failed_before_start"
        and a.details.get("error_class") == "ExecutorShutdown"
    ]
    assert len(shutdown) == 1
    assert shutdown[0].details["report_month"] == "2026-03"

    # A late queue call for the same job is deduplicated — still one row.
    executor.queue_failed_start_audit(
        tenant_id=TENANT,
        connector_key="youtube_reporting",
        account_id="acct-1",
        report_month="2026-03",
        error_class="RuntimeError",
        actor_identity=ACTOR,
    )
    with factory() as session:
        audits = session.scalars(select(AuditLogORM)).all()
    assert len(audits) == 1


def test_close_drops_uncommitted_leftover_reservation_without_audit(tmp_path, monkeypatch) -> None:
    """An unmarked leftover reservation is dropped without a failure row.

    A reservation exists BEFORE the request transaction commits; one still
    pending after grace with no committed mark belongs to a still-open or
    rolled-back request — auditing it would invent a failure for a job that
    was never accepted.
    """
    from ums_smart_revenue.connectors.runs import executor as executor_module

    monkeypatch.setattr(executor_module, "_PENDING_HOOK_GRACE_SECONDS", 0.05)
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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
    executor.close()

    with factory() as session:
        audits = session.scalars(select(AuditLogORM)).all()
    assert not any(
        a.details.get("action") == "job_failed_before_start" for a in audits
    )


def test_close_keeps_audit_gate_open_for_hook_past_reservation_removal(tmp_path) -> None:
    """close() holds the audit pool until a hook's failure audit is queued.

    Regression for the interleaving where activate() has already popped the
    _SlotReservation from the registry before the hook calls
    queue_failed_start_audit: the registry alone shows zero pending slots,
    so only the begin/end_post_commit bracket keeps close() from disabling
    audit submission mid-hook.
    """
    import threading

    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(session_factory=factory, max_workers=1, stale_running_hours=6)
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

    # Start close() so it enters the grace window with the slot pending,
    # then run the hook inline: activate raises against the stopped pool,
    # pops the slot, and the audit is queued while close() is still waiting.
    closer = threading.Thread(target=executor.close)
    closer.start()

    executor.begin_post_commit(reservation)
    try:
        executor.activate(reservation)
        raise AssertionError("activate must fail against a stopped pool")
    except RuntimeError as exc:
        executor.cancel_reservation(reservation)
        executor.queue_failed_start_audit(
            tenant_id=reservation.tenant_id,
            connector_key=reservation.connector_key,
            account_id=reservation.account_id,
            report_month=reservation.report_month,
            error_class=type(exc).__name__,
            actor_identity=reservation.actor_identity,
        )
    finally:
        executor.end_post_commit(reservation)

    closer.join(timeout=10)
    assert not closer.is_alive(), "close() must wait for the in-flight hook"

    with factory() as session:
        audits = session.scalars(select(AuditLogORM)).all()
    failure_rows = [
        a for a in audits if a.details.get("action") == "job_failed_before_start"
    ]
    assert len(failure_rows) == 1
    assert failure_rows[0].details["error_class"] == "RuntimeError"
    assert failure_rows[0].details["report_month"] == "2026-03"


# ============================================================================
# Purpose: Regression for the late-commit interleaving — a request that
#   commits AFTER close()'s grace window still needs its
#   job_failed_before_start row; the post-close queue path must write via the
#   last-chance writer rather than drop the audit.
# Database/ORM: audit_logs on the disposable file-backed SQLite engine.
# Standards: proves the accepted-202 lifecycle edge survives shutdown on the
#   strictest engine (SQLite), where a same-engine synchronous write inside
#   after_commit would deadlock against the still-held request connection.
# Blast Radius: Test-only — guards the post-close audit delivery path.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     queue_failed_start_audit post-close fallback.
# ============================================================================
def test_post_close_commit_still_lands_failure_audit(tmp_path, monkeypatch) -> None:
    """A hook firing after close() still persists its failure audit.

    The reservation's request outlives the shutdown grace, commits, and its
    after_commit hook runs after the audit pool closed — the audit row must
    still land instead of being dropped.
    """
    import time

    from ums_smart_revenue.connectors.runs import executor as executor_module

    monkeypatch.setattr(executor_module, "_PENDING_HOOK_GRACE_SECONDS", 0.05)
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
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
    # Shutdown runs its full course while the request transaction is still
    # open: grace expires and the uncommitted leftover is dropped un-audited.
    executor.close()

    # The request then commits and its after_commit hook runs post-close.
    executor.begin_post_commit(reservation)
    try:
        executor.activate(reservation)
        raise AssertionError("activate must fail after the registry was cleared")
    except RuntimeError as exc:
        executor.cancel_reservation(reservation)
        executor.queue_failed_start_audit(
            tenant_id=reservation.tenant_id,
            connector_key=reservation.connector_key,
            account_id=reservation.account_id,
            report_month=reservation.report_month,
            error_class=type(exc).__name__,
            actor_identity=reservation.actor_identity,
        )
    finally:
        executor.end_post_commit(reservation)

    # The last-chance writer is asynchronous — poll for the row.
    deadline = time.monotonic() + 5
    while True:
        with factory() as session:
            rows = session.scalars(select(AuditLogORM)).all()
        failures = [
            a for a in rows if a.details.get("action") == "job_failed_before_start"
        ]
        if failures:
            break
        assert time.monotonic() < deadline, "post-close audit row never landed"
        time.sleep(0.05)
    assert len(failures) == 1
    assert failures[0].details["report_month"] == "2026-03"
