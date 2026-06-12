"""In-process bounded executor that runs connector pulls off the request thread."""
from __future__ import annotations

import logging
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    run_one,
)
from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.session import SessionFactory

logger = logging.getLogger(__name__)

_JobKey = tuple[UUID, str, str, str]


@dataclass(frozen=True)
class ConnectorJobActor:
    """Minimal, thread-safe snapshot of the submitting principal for the worker.

    The worker thread cannot share the request's UserPrincipal safely across the
    thread boundary, so the route passes this immutable snapshot and the worker
    rebuilds a UserPrincipal carrying RUN_CONNECTOR_JOBS@global for the
    Bucket-A failure audit (attribution preserved via the audit reason + the
    sink's unknown-actor stash in details['actor_user_id']).
    """

    user_id: str
    email: str


@dataclass(frozen=True)
class _SlotReservation:
    """Pre-claim for a registry slot whose worker has not yet been enqueued.

    The route reserves a slot via ``submit_if_absent`` BEFORE writing the
    route-owned audit row; the worker is enqueued only after the audit row
    commits (via an ``after_commit`` hook that calls ``activate``). On a
    rollback the route invokes ``cancel_reservation`` to drop the claim,
    so the registry can never deadlock on a half-committed submission.
    """

    key: _JobKey
    tenant_id: UUID
    connector_key: str
    account_id: str
    report_month: str
    dry_run: bool
    triggered_by_user_id: UUID | None
    actor_identity: ConnectorJobActor


# ============================================================================
# Purpose: Own a bounded ThreadPoolExecutor + an in-process registry of live
#   jobs keyed (tenant, connector_key, account_id, report_month), and run each
#   submitted connector pull on its OWN session under connector_tenant_context
#   (re-establishing TENANT_CTX in the worker thread, which does not inherit the
#   request contextvar). Mirrors the TenantResolverMiddleware executor pattern
#   (weakref.finalize GC backstop + explicit close()).
#
#   The public submission API is ``submit_if_absent`` -> ``activate``
#   (or ``cancel_reservation``) so the route can hold a registry slot
#   BEFORE the audit row commits and only enqueue the worker AFTER the
#   commit succeeds. This makes a failed audit commit a no-op for the
#   worker (no orphan run, no run-history row, no live credential refresh)
#   and prevents the previous check-then-act duplicate race (where two
#   concurrent requests could both pass ``has_active_job`` before either
#   reached ``submit``).
#
#   A dry-run job is still a live worker; the worker calls ``run_one``
#   with ``dry_run=True``, captures the ``ConnectorRunOutcome`` (run is
#   None for dry runs), and audits it as ``job_dry_run_completed`` so
#   operators have counts + per-report-failure detail to inspect instead
#   of just the green ``submitted`` signal.
#
# Database/ORM: opens its own Session via session_factory; run_one writes
#   connector_runs + audit_logs; the Bucket-A catch writes one CONNECTOR_JOB_RUN
#   audit row via SqlAlchemyAuditSink on a fresh own session, wrapped in
#   platform_lane (audit_logs is a TENANT_PLATFORM_ONLY_WRITE table -> a
#   tenant-lane write would InsufficientPrivilege-deny on Postgres; SQLite no-op).
# Standards: never wraps run_one in platform_lane (not nest-safe; run_one owns
#   its OWN elevation internally) -- platform_lane is used ONLY for the separate
#   Bucket-A audit that runs OUTSIDE run_one. Worker NEVER propagates out of the
#   thread: Bucket-A errors are audited (canned class name only), everything else
#   is logged. Registry key removed in finally on every path.
# Blast Radius: Authorization (tenant-pinned worker), audit (additive
#   job_failed_before_start / job_dry_run_completed), connector run lifecycle.
#   No finance math change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> executor +
#     weakref.finalize + close() precedent.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     connector_tenant_context replays the ACTIVE-only tenant gate.
#   - File: scripts/run_google_connector.py -> the CLI pattern this reuses.
#   - File: backend/ums_smart_revenue/api/connectors.py -> the route uses
#     submit_if_absent + after_commit.activate / after_rollback.cancel_reservation.
# ============================================================================
class ConnectorJobExecutor:
    """Bounded in-process runner for connector pull jobs with a dup registry.

    Registry values are either a :class:`Future` (the worker has been
    enqueued) or a :class:`_SlotReservation` (the route has claimed the
    slot but the audit row has not yet committed). Both count as
    ``active`` for dedup purposes (``has_active_job`` checks membership).
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        max_workers: int,
        stale_running_hours: int,
    ) -> None:
        """Build the pool, the registry lock, and the GC-safe shutdown backstop."""
        self._session_factory = session_factory
        self._stale_running_hours = stale_running_hours
        self._lock = threading.Lock()
        self._registry: dict[_JobKey, Future | _SlotReservation] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ums-connector-job",
        )
        self._finalizer = weakref.finalize(
            self,
            self._executor.shutdown,
            wait=False,
            cancel_futures=True,
        )

    def close(self) -> None:
        """Shut the pool down deterministically (called from the app lifespan)."""
        self._finalizer()

    def has_active_job(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
    ) -> bool:
        """Return whether a live Future or pending reservation exists for the scope."""
        key = (tenant_id, connector_key, account_id, report_month)
        with self._lock:
            return key in self._registry

    # ------------------------------------------------------------------
    # Atomic check + reserve: replaces the previous has_active_job + submit
    # pair so two concurrent requests cannot both pass the dup check.
    # ------------------------------------------------------------------
    def submit_if_absent(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        dry_run: bool,
        triggered_by_user_id: UUID | None,
        actor_identity: ConnectorJobActor,
    ) -> _SlotReservation | None:
        """Reserve a slot for the scope; return None if a slot is already held.

        The reservation is NOT yet a Future -- the worker is enqueued only
        after the caller invokes :meth:`activate` (typically from an
        SQLAlchemy ``after_commit`` hook). A failed commit drops the
        reservation via :meth:`cancel_reservation`, so the registry can
        never deadlock on a half-committed submission.

        Returns ``None`` when the scope is already held (either a pending
        reservation or an active Future). Holding the lock across the
        check + insert is the atomic guard against concurrent dup
        submissions: a worker that has just finished cannot deregister
        its key until this call releases the lock, so the second
        concurrent request will see the in-flight slot.
        """
        key = (tenant_id, connector_key, account_id, report_month)
        reservation = _SlotReservation(
            key=key,
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            report_month=report_month,
            dry_run=dry_run,
            triggered_by_user_id=triggered_by_user_id,
            actor_identity=actor_identity,
        )
        with self._lock:
            if key in self._registry:
                return None
            self._registry[key] = reservation
        return reservation

    def activate(self, reservation: _SlotReservation) -> Future:
        """Replace a reservation with a real ``Future`` and enqueue the worker.

        Idempotent: returns the existing ``Future`` if the reservation was
        already activated. Raises ``RuntimeError`` if the registry no
        longer holds the reservation (e.g. it was cancelled or replaced
        by a re-submission with the same key).
        """
        key = reservation.key
        with self._lock:
            current = self._registry.get(key)
            if isinstance(current, Future):
                return current
            if current is not reservation:
                raise RuntimeError(
                    f"reservation for {key} was deregistered or replaced"
                )
            future = self._executor.submit(
                self._run_job,
                tenant_id=reservation.tenant_id,
                connector_key=reservation.connector_key,
                account_id=reservation.account_id,
                report_month=reservation.report_month,
                dry_run=reservation.dry_run,
                triggered_by_user_id=reservation.triggered_by_user_id,
                actor_identity=reservation.actor_identity,
            )
            self._registry[key] = future
        return future

    def cancel_reservation(self, reservation: _SlotReservation) -> bool:
        """Drop a pending reservation; no-op if it was already activated.

        Returns ``True`` if the reservation was the live registry value
        and was dropped; ``False`` if the registry already held a
        ``Future`` (caller raced an ``activate`` and lost) or the key was
        no longer in the registry.
        """
        with self._lock:
            current = self._registry.get(reservation.key)
            if current is reservation:
                self._registry.pop(reservation.key, None)
                return True
        return False

    def _register(self, key: _JobKey) -> None:
        """Reserve a registry slot before submission (caller holds no lock).

        Retained for the unit tests that build a slot by hand; new callers
        should use :meth:`submit_if_absent` so the check + insert is atomic.
        """
        with self._lock:
            self._registry[key] = Future()

    def _deregister(self, key: _JobKey) -> None:
        """Drop a registry slot on worker completion."""
        with self._lock:
            self._registry.pop(key, None)

    def submit(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        dry_run: bool,
        triggered_by_user_id: UUID | None,
        actor_identity: ConnectorJobActor,
    ) -> Future:
        """Register the scope and submit the pull to the worker pool.

        Retained for direct-call sites that do not need the
        reserve-then-activate flow (notably the existing executor unit
        tests). The route uses :meth:`submit_if_absent` instead so the
        check + insert is atomic across concurrent requests.
        """
        key = (tenant_id, connector_key, account_id, report_month)
        # Register the REAL future atomically under the lock: enqueue while
        # holding the lock so a fast worker's finally->_deregister blocks until
        # this entry is set, then pops it. The previous register-placeholder ->
        # submit -> overwrite-after sequence had a race: a worker that finished
        # and deregistered BEFORE the overwrite would have a completed future
        # re-inserted, wedging has_active_job at True forever. ThreadPoolExecutor
        # .submit only enqueues (never blocks on a full pool), so holding the
        # lock across it is brief and deadlock-free.
        with self._lock:
            future = self._executor.submit(
                self._run_job,
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                dry_run=dry_run,
                triggered_by_user_id=triggered_by_user_id,
                actor_identity=actor_identity,
            )
            self._registry[key] = future
        return future

    def _run_job(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        dry_run: bool,
        triggered_by_user_id: UUID | None,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Worker body: own session -> tenant context -> run_one; fail-closed."""
        key = (tenant_id, connector_key, account_id, report_month)
        outcome: ConnectorRunOutcome | None = None
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    outcome = run_one(
                        session,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        account_id=account_id,
                        report_month=report_month,
                        dry_run=dry_run,
                        triggered_by_user_id=triggered_by_user_id,
                    )
            if dry_run and outcome is not None:
                # FIX: dry-run writes no connector_runs row (run_one skips
                # start_run entirely), so the only durable record of what
                # the dry-run found is the executor-side outcome. Audit one
                # CONNECTOR_JOB_RUN row with the counts and per-report
                # failures so operators inspecting the audit log can see
                # which reports would fail without re-running the dry-run.
                self._audit_dry_run_outcome(
                    tenant_id=tenant_id,
                    connector_key=connector_key,
                    account_id=account_id,
                    report_month=report_month,
                    outcome=outcome,
                    actor_identity=actor_identity,
                )
        except GoogleConnectorError as exc:
            # FIX: ConnectorServicePrincipalUnavailableError is a
            # GoogleConnectorError subclass; it lands here and is audited
            # as a Bucket-A job_failed_before_start row. Previously this
            # path raised ValueError and was swallowed by the catch-all
            # branch below, leaving a 202 with no run row, no failure
            # audit, and no operator-visible reason the job never started.
            logger.exception(
                "Connector job failed before start (tenant=%s connector=%s)",
                tenant_id,
                connector_key,
            )
            self._audit_failed_before_start(
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                error_class=type(exc).__name__,
                actor_identity=actor_identity,
            )
        except Exception:  # noqa: BLE001 — fail-closed: never escape the thread
            logger.exception(
                "Connector job worker raised after start (tenant=%s connector=%s)",
                tenant_id,
                connector_key,
            )
        finally:
            self._deregister(key)

    def _audit_failed_before_start(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Write ONE CONNECTOR_JOB_RUN job_failed_before_start row, fresh session."""
        actor = self._build_audit_actor(tenant_id=tenant_id, actor_identity=actor_identity)
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    # audit_logs is platform-only-write: elevate to app_platform
                    # for this standalone audit (run_one does its own elevation;
                    # this audit runs OUTSIDE run_one). No-op off Postgres.
                    with platform_lane(session):
                        sink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
                        record_audit_event(
                            sink=sink,
                            actor=actor,
                            event_type=AuditEventType.CONNECTOR_JOB_RUN,
                            entity_type="api_connector",
                            entity_id=f"{connector_key}:{account_id}",
                            scope=AccessScope.connector(connector_key),
                            reason="connector job failed before start",
                            details={
                                "action": "job_failed_before_start",
                                "report_month": report_month,
                                "error_class": error_class,
                            },
                        )
                        session.commit()
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist job_failed_before_start audit (tenant=%s)",
                tenant_id,
            )

    def _audit_dry_run_outcome(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        outcome: ConnectorRunOutcome,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Write ONE CONNECTOR_JOB_RUN job_dry_run_completed row, fresh session.

        Dry-run jobs do not create a ``connector_runs`` row, so this audit
        row is the only durable record of what the dry-run found. The
        counts mirror the B2.3 ``CONNECTOR_RUN_COUNT_KEYS`` shape that
        ``finish_run`` validates; per-report failures are listed as a
        ``[{"report_type": ..., "error_class": ...}, ...]`` array so an
        operator console can render them directly.
        """
        actor = self._build_audit_actor(tenant_id=tenant_id, actor_identity=actor_identity)
        per_report_failures = [
            {"report_type": report_type, "error_class": error_class}
            for report_type, error_class in outcome.per_report_failures
        ]
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    with platform_lane(session):
                        sink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
                        record_audit_event(
                            sink=sink,
                            actor=actor,
                            event_type=AuditEventType.CONNECTOR_JOB_RUN,
                            entity_type="api_connector",
                            entity_id=f"{connector_key}:{account_id}",
                            scope=AccessScope.connector(connector_key),
                            reason="connector dry-run completed",
                            details={
                                "action": "job_dry_run_completed",
                                "report_month": report_month,
                                "dry_run": True,
                                "counts": dict(outcome.counts),
                                "per_report_failures": per_report_failures,
                            },
                        )
                        session.commit()
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist job_dry_run_completed audit (tenant=%s)",
                tenant_id,
            )

    def _build_audit_actor(
        self,
        *,
        tenant_id: UUID,
        actor_identity: ConnectorJobActor,
    ) -> UserPrincipal:
        """Build the tenant-pinned ``UserPrincipal`` for an executor-owned audit row.

        Shared between ``_audit_failed_before_start`` [Bucket-A
        pre-start failure] and ``_audit_dry_run_outcome`` [dry-run
        outcome persistence]. The principal carries
        ``RUN_CONNECTOR_JOBS@global`` so the audit log shows the
        executor's anonymous system identity for the row, with the
        submitting user preserved via the reason text and the
        SqlAlchemyAuditSink's unknown-actor stash in
        ``details["actor_user_id"]`` (matches the Bucket-A audit
        precedent).
        """
        return UserPrincipal(
            user_id=actor_identity.user_id,
            email=actor_identity.email,
            direct_permissions=(
                PermissionGrant(
                    permission=Permission.RUN_CONNECTOR_JOBS,
                    scope=AccessScope.global_scope(),
                    active=True,
                ),
            ),
            tenant_id=str(tenant_id),
        )
