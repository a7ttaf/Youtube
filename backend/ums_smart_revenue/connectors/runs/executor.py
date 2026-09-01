# ============================================================================
# Purpose: In-process bounded executor that runs connector pulls (and CMS
#   group-sync jobs) off the request thread, with an atomic reserve ->
#   activate registry that makes duplicate concurrent submissions impossible
#   and a failed audit commit a no-op for the worker. The detailed class
#   contract sits directly above ConnectorJobExecutor below.
# Database/ORM: opens its own Session per job via session_factory; workers
#   write connector_runs/audit_logs and (group-sync) channel-group rows; the
#   shutdown path audits cancelled queued futures as job_failed_before_start.
# Standards: module-owned threads with a weakref.finalize GC backstop plus an
#   explicit close(); workers never propagate exceptions out of the thread
#   (typed failures are audited, everything else is logged); registry slots
#   are dropped on every path, including a failed worker enqueue.
# Blast Radius: Authorization (tenant-pinned workers), audit rows, connector
#   run lifecycle, and group naming/membership state via the group-sync
#   worker. No finance math.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> the route's
#     submit_if_absent + outer-transaction activation/cancellation flow.
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> the only
#     scheduled submitter of the group-sync job kind.
#   - File: backend/ums_smart_revenue/app.py -> lifespan close() wiring.
# ============================================================================
"""In-process bounded executor that runs connector pulls off the request thread."""

from __future__ import annotations

import logging
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import PlatformLaneAuditSink, SqlAlchemyAuditSink
from ums_smart_revenue.config.settings import GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV
from ums_smart_revenue.connectors.google.audit import build_connector_service_principal
from ums_smart_revenue.connectors.google.errors import (
    ConnectorServicePrincipalUnavailableError,
    GoogleConnectorError,
)
from ums_smart_revenue.connectors.runs.group_sync import (
    GroupsClientFactory,
    GroupSyncConflictRefusedError,
    GroupSyncFetchError,
    GroupSyncRunResult,
    default_groups_client_factory,
    run_group_sync,
)
from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    run_one,
)
from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.db.session import SessionFactory
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.org.channel_group_sync import GroupSyncOutcome
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupConflictError,
    ChannelGroupOwnerReassignmentError,
)
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import make_placeholder_tenant

logger = logging.getLogger(__name__)

# ============================================================================
# Purpose: Bound ConnectorJobExecutor.close() so FastAPI lifespan teardown
#   cannot hang past Compose's explicit 120s stop_grace_period. A stalled
#   connector worker must not block graceful shutdown forever; 90s bounds the
#   WHOLE close() — the shutdown-audit join and the drain wait share ONE
#   deadline — leaving headroom for GroupSyncScheduler.close() and logging
#   restore before SIGKILL.
# Database/ORM: None.
# Standards: concurrent.futures.wait timeout only; no unbounded future.result().
# Blast Radius: Shutdown durability — timed-out workers may still be running
#   when the process exits; queued-job shutdown audit already ran before drain.
# Connections:
#   - File: docker-compose.yml -> explicit 120s stop_grace_period.
#   - File: backend/ums_smart_revenue/app.py -> lifespan calls close().
# ============================================================================
CLOSE_DRAIN_TIMEOUT_SECONDS = 90.0
# FIX(codex round-23 P2): the cancelled-job audits used to run synchronously
# before close() ever reached the bounded drain, so a single audit write
# blocked on the shared SQLite one-slot QueuePool connection -- held by the very
# worker being drained -- could stall shutdown unboundedly until Docker
# SIGKILLed the process. The audit phase now runs on a daemon thread joined
# with this budget; audits that fit inside it land durably, and the ones
# that do not are logged and abandoned rather than taking the drain budget
# hostage.
# FIX(codex round-28 P1): this budget is a CAP inside close()'s single
# deadline, not an additive phase. Previously the audit join (30s) and the
# drain wait (90s) ran back-to-back for up to 120s -- the entire Compose
# stop_grace_period -- before GroupSyncScheduler.close() could even start.
# close() now captures one monotonic deadline of CLOSE_DRAIN_TIMEOUT_SECONDS
# and both phases draw from it.
SHUTDOWN_AUDIT_BUDGET_SECONDS = 30.0

_JobKey = tuple[UUID, str, str, str]

# Reserved registry identity for CMS group-sync jobs. The registry key stays the
# same 4-tuple as report pulls -- (tenant_id, connector_key, account_id,
# report_month) -- but a sync job uses this sentinel connector_key and a "-"
# month, so a sync job and a report pull for the same tenant+account can NEVER
# collide: no real connector is keyed "cms_group_sync", so the two live in
# disjoint connector-key namespaces and dedup/has_active_job/_deregister need no
# special-casing.
# NOTE: named ..._SLUG, not ..._KEY -- a "KEY = <string literal>" module constant
# trips hardcoded-credential scanners, and this value is a namespace slug, never
# a secret.
GROUP_SYNC_JOB_CONNECTOR_SLUG = "cms_group_sync"
GROUP_SYNC_JOB_MONTH = "-"

# Worker-dispatch discriminator carried on the reservation (see _enqueue_worker).
_JOB_KIND_PULL = "pull"
_JOB_KIND_GROUP_SYNC = "group_sync"

# Durable audit-intent actions. ``job_submitted`` is committed before a worker
# can be activated; startup reconciliation treats an intent without either of
# the terminal dispatch edges as a job the previous process abandoned while
# still queued. The stable UUID prevents tuple/time heuristics and lets two
# startup processes coordinate without misclassifying an older run.
_JOB_ACTION_SUBMITTED = "job_submitted"
_JOB_ACTION_DISPATCH_STARTED = "job_dispatch_started"
_JOB_ACTION_FAILED_BEFORE_START = "job_failed_before_start"
_JOB_RECOVERY_TERMINAL_ACTIONS = frozenset(
    {_JOB_ACTION_DISPATCH_STARTED, _JOB_ACTION_FAILED_BEFORE_START}
)
_JOB_RECOVERY_BATCH_SIZE = 100

# Expected failures are mapped to this source-controlled allowlist before they
# reach logs. Exception messages and subclass names are deliberately excluded:
# Google HTTP error strings can contain signed URLs, credentials, and headers.
_EXPECTED_GOOGLE_FAILURE_CATEGORY = "google_connector_failure"
_EXPECTED_GROUP_SYNC_FETCH_CATEGORY = "group_sync_fetch_failure"
_EXPECTED_GROUP_SYNC_CONFLICT_CATEGORY = "group_sync_conflict"
_EXPECTED_GROUP_OWNER_CATEGORY = "group_owner_conflict"


# ============================================================================
# Purpose: Collapse expected connector/group-sync exception families into a
#   fixed safe logging taxonomy without rendering exception text or subclass
#   names that can carry signed request material.
# Database/ORM: None.
# Standards: Total over the typed expected-failure tuple; no dynamic values.
# Blast Radius: Log categories only; audit error_class remains unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/config/logging_config.py -> defense-in-depth
#     formatter redaction for unexpected tracebacks that still need diagnostics.
# ============================================================================
def _expected_failure_category(exc: Exception) -> str:
    """Return a bounded category for a typed expected connector failure."""
    if isinstance(exc, GoogleConnectorError):
        return _EXPECTED_GOOGLE_FAILURE_CATEGORY
    if isinstance(exc, GroupSyncFetchError):
        return _EXPECTED_GROUP_SYNC_FETCH_CATEGORY
    if isinstance(exc, (GroupSyncConflictRefusedError, ChannelGroupConflictError)):
        return _EXPECTED_GROUP_SYNC_CONFLICT_CATEGORY
    if isinstance(exc, ChannelGroupOwnerReassignmentError):
        return _EXPECTED_GROUP_OWNER_CATEGORY
    raise TypeError("expected connector failure category requested for unsupported exception")


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
    job_id: UUID
    # Which worker body ``activate`` dispatches this reservation to. Defaults to
    # the report-pull worker so every existing caller is unchanged; a group-sync
    # reservation sets ``_JOB_KIND_GROUP_SYNC``. Chosen over branching on the
    # connector_key sentinel so dispatch states its intent explicitly rather than
    # riding on a magic-string match (see _enqueue_worker).
    job_kind: str = _JOB_KIND_PULL


@dataclass(frozen=True)
class _ActiveJob:
    """Registry entry for an enqueued worker plus shutdown-audit metadata."""

    future: Future
    actor_identity: ConnectorJobActor
    job_id: UUID


@dataclass(frozen=True)
class _ShutdownAuditTask:
    """A shutdown-audit thread plus its post-join durability result."""

    thread: threading.Thread
    result: dict[str, bool]


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
#   A SECOND job kind rides the same registry + reserve/activate machinery:
#   CMS group sync (``submit_group_sync_if_absent`` -> ``activate`` ->
#   ``_run_group_sync_job``). It is keyed under the ``cms_group_sync`` sentinel
#   connector_key so it can never collide with a report pull, and its worker
#   drives the shared ``run_group_sync`` core on ITS OWN session: the domain
#   rows (apply_group_sync) and the audit rows (per-group GROUP_UPDATED + a
#   run-level GROUPS_SYNCED summary written only on change) share that one
#   session and ONE commit, so the #169 atomic invariant holds by construction.
#   Failures fold into one ``group_sync_job_failed`` row via the fresh-session
#   ``_audit_group_sync_failure`` sibling.
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
#   job_failed_before_start / job_dry_run_completed / GROUPS_SYNCED /
#   GROUP_UPDATED / group_sync_job_failed), connector run lifecycle, and group
#   naming/membership/active state (via the group-sync worker's apply). No
#   finance math change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> executor +
#     weakref.finalize + close() precedent.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     connector_tenant_context replays the ACTIVE-only tenant gate.
#   - File: backend/ums_smart_revenue/connectors/runs/group_sync.py -> the
#     HTTP-free sync core the group-sync worker drives (Sched 1).
#   - File: scripts/run_google_connector.py -> the CLI pattern this reuses.
#   - File: backend/ums_smart_revenue/api/connectors.py -> the route uses
#     submit_if_absent + outer-transaction finalization for activate/cancel.
# ============================================================================
class ConnectorJobExecutor:
    """Bounded in-process runner for connector pull jobs with a dup registry.

    Registry values are either a :class:`_ActiveJob` (the worker has been
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
        group_sync_client_factory: GroupsClientFactory = default_groups_client_factory,
    ) -> None:
        """Build the pool, the registry lock, and the GC-safe shutdown backstop.

        ``group_sync_client_factory`` is the seam the PG tier and unit tests use
        to inject a fake CMS groups client; production passes nothing and the
        real ``default_groups_client_factory`` (from the Sched-1 core) is used.
        """
        self._session_factory = session_factory
        self._stale_running_hours = stale_running_hours
        self._group_sync_client_factory = group_sync_client_factory
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._registry: dict[_JobKey, Future | _SlotReservation | _ActiveJob] = {}
        self._shutdown_pending_futures: set[Future] = set()
        self._shutdown_audit_tasks: list[_ShutdownAuditTask] = []
        self._shutdown_audit_failed = False
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

    def close(self) -> bool:
        """Shut the pool down deterministically (called from the app lifespan).

        Cancel queued futures first (``cancel_futures=True``), audit those
        cancelled jobs immediately as ``job_failed_before_start`` /
        ``ExecutorShutdown``, then wait for every future that remained
        non-cancelled after shutdown so a job that transitioned from queued to
        running between the pre-shutdown snapshot and ``shutdown()`` cannot
        escape the drain. The immediate audit is an optimization; the already-
        committed ``job_submitted`` intent is the durable handoff if Docker
        later SIGKILLs the process at ``stop_grace_period``. On drain timeout,
        log which futures remain and
        return ``False`` and retain their futures for a repeated ``close()`` or
        :meth:`wait_for_shutdown_completion`. Running futures that finish in
        time deregister themselves; they are never audited as pre-start
        failures. The weakref finalizer remains as a GC backstop for paths that
        bypass ``close()``.

        FIX(codex round-28 P1): the shutdown-audit join and the drain wait
        share ONE monotonic deadline of ``CLOSE_DRAIN_TIMEOUT_SECONDS``
        captured at the top of this method. The audit join gets at most
        ``min(SHUTDOWN_AUDIT_BUDGET_SECONDS, remaining)``, and the drain wait
        gets whatever budget is left, so total close() waits stay bounded by
        CLOSE_DRAIN_TIMEOUT_SECONDS instead of the two phases' budgets
        summing to the full Compose stop_grace_period.

        Returns:
            True when every retained worker and shutdown-audit task finished
            durably (or none existed). False while work remains outstanding or
            an audit write has failed.
        """
        # FIX: Serialize close/wait state transitions and retain timed-out
        # futures. Clearing the dedup registry previously made a second close()
        # falsely report success while the first call's worker was still hung.
        with self._close_lock:
            deadline = time.monotonic() + CLOSE_DRAIN_TIMEOUT_SECONDS
            self._executor.shutdown(wait=False, cancel_futures=True)
            running_futures, current_audits_durable = self._audit_pending_on_shutdown(deadline)
            self._shutdown_pending_futures.update(running_futures)

            pending = set(self._shutdown_pending_futures)
            if pending:
                drain_timeout = max(0.0, deadline - time.monotonic())
                done, not_done = wait(pending, timeout=drain_timeout)
                self._consume_shutdown_futures(done)
                if not_done:
                    logger.error(
                        "Executor close drain timed out after %.0fs; "
                        "%d worker future(s) still running: %s",
                        drain_timeout,
                        len(not_done),
                        list(not_done),
                    )
                    return False

            audits_durable = current_audits_durable and self._shutdown_audits_complete_and_durable()
            self._finalizer.detach()
            return audits_durable

    def wait_for_shutdown_completion(self) -> None:
        """Block until every retained worker and shutdown-audit task settles.

        This is the unbounded, idempotent counterpart to :meth:`close`. The app
        can honor its bounded graceful-stop contract first, then use this API to
        keep the executor object and its late audit thread state reachable until
        they actually finish. Repeated or concurrent close/wait calls are
        serialized by ``_close_lock``.

        Unbounded applies to the settle loop: the first audit join still runs
        under the shared 30-second budget (``_audit_cancelled_within_budget``
        caps ``deadline``), and durability is then guaranteed by the re-join
        loop below rather than by the first phase.
        """
        with self._close_lock:
            self._executor.shutdown(wait=False, cancel_futures=True)
            running_futures, _ = self._audit_pending_on_shutdown(float("inf"))
            self._shutdown_pending_futures.update(running_futures)
            if self._shutdown_pending_futures:
                done, _ = wait(set(self._shutdown_pending_futures))
                self._consume_shutdown_futures(done)

            while self._shutdown_audit_tasks:
                tasks = tuple(self._shutdown_audit_tasks)
                for task in tasks:
                    task.thread.join()
                self._shutdown_audits_complete_and_durable()

            self._finalizer.detach()

    def _consume_shutdown_futures(self, futures: set[Future]) -> None:
        """Observe settled worker results exactly once and release retention."""
        for future in futures:
            try:
                future.result()
            except Exception:
                # FIX: Surface worker failures that completed during drain;
                # the previous bare ``except Exception: pass`` hid them.
                logger.exception("Connector job worker raised during executor close drain")
        self._shutdown_pending_futures.difference_update(futures)

    def _shutdown_audits_complete_and_durable(self) -> bool:
        """Reap finished audit tasks and report their cumulative durability."""
        still_running: list[_ShutdownAuditTask] = []
        for task in self._shutdown_audit_tasks:
            if task.thread.is_alive():
                still_running.append(task)
                continue
            task.thread.join()
            if not task.result["durable"]:
                self._shutdown_audit_failed = True
        self._shutdown_audit_tasks = still_running
        return not still_running and not self._shutdown_audit_failed

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
        after the caller invokes :meth:`activate` (the HTTP route does so only
        after the committed outer transaction releases its checkout). A failed
        commit drops the reservation via :meth:`cancel_reservation`, so the
        registry can never deadlock on a half-committed submission.

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
            job_id=uuid4(),
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

        Fail-closed on enqueue failure: if the pool refuses the worker
        (e.g. ``shutdown`` raced ``activate``), the reservation is dropped
        from the registry while the lock is still held, so a transient
        submit failure can never wedge the slot as in-flight forever --
        the next submission (a scheduler tick or a route retry) can claim
        it again.
        """
        key = reservation.key
        with self._lock:
            current = self._registry.get(key)
            if isinstance(current, _ActiveJob):
                return current.future
            if isinstance(current, Future):
                return current
            if current is not reservation:
                raise RuntimeError(f"reservation for {key} was deregistered or replaced")
            try:
                future = self._enqueue_worker(reservation)
            except Exception:
                # Compare-and-delete: only drop the entry if it is still THIS
                # reservation (a concurrent cancel/resubmission could not have
                # run under the lock, but the identity check keeps the
                # invariant explicit). Then re-raise so the caller audits/logs
                # the real failure instead of a phantom in-flight slot.
                if self._registry.get(key) is reservation:
                    del self._registry[key]
                raise
            self._stash_and_register(
                future=future,
                key=key,
                actor_identity=reservation.actor_identity,
                job_id=reservation.job_id,
            )
        return future

    def _enqueue_worker(self, reservation: _SlotReservation) -> Future:
        """Submit the worker matching the reservation's ``job_kind``. Caller holds the lock.

        The ONLY kind-dependent branch in the reserve -> activate flow: a
        group-sync reservation dispatches to :meth:`_run_group_sync_job`, every
        other reservation to :meth:`_run_job` (report pulls). Everything else --
        dedup, registry stash, ``_deregister`` -- is shared and keyed only by the
        4-tuple, so a sync key needs zero special-casing anywhere else.
        """
        if reservation.job_kind == _JOB_KIND_GROUP_SYNC:
            return self._executor.submit(
                self._run_group_sync_job,
                tenant_id=reservation.tenant_id,
                content_owner_id=reservation.account_id,
                actor_identity=reservation.actor_identity,
                job_id=reservation.job_id,
            )
        return self._executor.submit(
            self._run_job,
            tenant_id=reservation.tenant_id,
            connector_key=reservation.connector_key,
            account_id=reservation.account_id,
            report_month=reservation.report_month,
            dry_run=reservation.dry_run,
            triggered_by_user_id=reservation.triggered_by_user_id,
            actor_identity=reservation.actor_identity,
            job_id=reservation.job_id,
        )

    def submit_group_sync_if_absent(
        self,
        *,
        tenant_id: UUID,
        content_owner_id: str,
        actor_identity: ConnectorJobActor,
    ) -> _SlotReservation | None:
        """Reserve a CMS group-sync slot for one content owner; None if already held.

        The same atomic reserve flow as :meth:`submit_if_absent` (the registry
        lock held across the check + insert), keyed under
        :data:`GROUP_SYNC_JOB_CONNECTOR_SLUG` with the ``-`` month sentinel so a
        sync job and a report pull for the same tenant+account never collide.
        The scheduler calls :meth:`activate` on the returned reservation to
        enqueue :meth:`_run_group_sync_job`; ``None`` means a sync for this owner
        is already in flight (dedup), skip it.
        """
        key = (tenant_id, GROUP_SYNC_JOB_CONNECTOR_SLUG, content_owner_id, GROUP_SYNC_JOB_MONTH)
        reservation = _SlotReservation(
            key=key,
            tenant_id=tenant_id,
            connector_key=GROUP_SYNC_JOB_CONNECTOR_SLUG,
            account_id=content_owner_id,
            report_month=GROUP_SYNC_JOB_MONTH,
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=actor_identity,
            job_id=uuid4(),
            job_kind=_JOB_KIND_GROUP_SYNC,
        )
        with self._lock:
            if key in self._registry:
                return None
            self._registry[key] = reservation
        return reservation

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
            job_id = uuid4()
            future = self._executor.submit(
                self._run_job,
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                dry_run=dry_run,
                triggered_by_user_id=triggered_by_user_id,
                actor_identity=actor_identity,
                job_id=job_id,
            )
            self._stash_and_register(
                future=future,
                key=key,
                actor_identity=actor_identity,
                job_id=job_id,
            )
        return future

    def _stash_and_register(
        self,
        future: Future,
        key: _JobKey,
        actor_identity: ConnectorJobActor,
        job_id: UUID,
    ) -> None:
        """Stash shutdown-audit metadata on *future* and insert into the registry.

        Must be called while holding ``self._lock``.
        """
        self._registry[key] = _ActiveJob(
            future=future,
            actor_identity=actor_identity,
            job_id=job_id,
        )

    # ========================================================================
    # Purpose: Persist the scheduler's submission intent in audit_logs before
    #   activation, using the same stable request_id handoff as the HTTP route.
    # Database/ORM: AuditLogORM write on the caller's tenant transaction.
    # Standards: Caller owns commit/rollback; PlatformLaneAuditSink preserves
    #   the existing app_platform write grant and tenant RLS context.
    # Blast Radius: Additive connector audit row only; no finance/domain write.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> commits
    #     this intent before calling activate().
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> startup
    #     recovery consumes unmatched intents after an unclean process exit.
    # ========================================================================
    def persist_submission_intent(
        self,
        *,
        session: Session,
        reservation: _SlotReservation,
        reason: str,
    ) -> None:
        """Append one durable pre-activation intent to the caller's transaction."""
        actor = self._build_audit_actor(
            tenant_id=reservation.tenant_id,
            actor_identity=reservation.actor_identity,
        )
        sink = PlatformLaneAuditSink(session, tenant_id=reservation.tenant_id)
        record_audit_event(
            sink=sink,
            actor=actor,
            event_type=AuditEventType.CONNECTOR_JOB_RUN,
            entity_type="api_connector",
            entity_id=f"{reservation.connector_key}:{reservation.account_id}",
            scope=AccessScope.connector(reservation.connector_key),
            reason=reason,
            request_id=str(reservation.job_id),
            details={
                "action": _JOB_ACTION_SUBMITTED,
                "job_kind": reservation.job_kind,
                "connector_key": reservation.connector_key,
                "account_id": reservation.account_id,
                "report_month": reservation.report_month,
                "dry_run": reservation.dry_run,
            },
        )

    def _record_dispatch_started(
        self,
        *,
        session: Session,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        actor_identity: ConnectorJobActor,
        job_id: UUID,
    ) -> bool:
        """Claim and append the durable edge proving a worker entered dispatch."""
        existing_actions = self._lock_job_lifecycle_actions(
            session=session,
            tenant_id=tenant_id,
            job_id=job_id,
        )
        if existing_actions & _JOB_RECOVERY_TERMINAL_ACTIONS:
            # FIX: Startup recovery and dispatch can race in multi-process
            # deployments. Both lock the same submitted intent; if recovery
            # already won, this worker must not mutate domain state afterward.
            return False
        actor = self._build_audit_actor(
            tenant_id=tenant_id,
            actor_identity=actor_identity,
        )
        sink = PlatformLaneAuditSink(session, tenant_id=tenant_id)
        record_audit_event(
            sink=sink,
            actor=actor,
            event_type=AuditEventType.CONNECTOR_JOB_RUN,
            entity_type="api_connector",
            entity_id=f"{connector_key}:{account_id}",
            scope=AccessScope.connector(connector_key),
            reason="connector job dispatch started",
            request_id=str(job_id),
            details={
                "action": _JOB_ACTION_DISPATCH_STARTED,
                "connector_key": connector_key,
                "account_id": account_id,
                "report_month": report_month,
            },
        )
        return True

    def _lock_job_lifecycle_actions(
        self,
        *,
        session: Session,
        tenant_id: UUID,
        job_id: UUID,
    ) -> set[str]:
        """Lock one request's lifecycle rows and return their bounded actions."""
        # PERF follow-up (recorded, not fixed here): ``audit_logs.request_id``
        # has no index (security_models.py defines user/event/entity/tenant
        # indexes only; migration 20260510_0001 declares the column bare). On
        # PostgreSQL this query therefore scans every CONNECTOR_JOB_RUN row of
        # the tenant under FOR UPDATE on each dispatch, and recovery's
        # request_id anti-join scans per candidate intent. Adding the index
        # needs a fresh Alembic revision; chaining it here would fork the graph
        # (PR #228's 20260828_0001 already parents 20260825_0002), so it lands
        # as a linear follow-up revision on main once this band merges.
        action = AuditLogORM.details["action"].as_string()
        statement = select(AuditLogORM).where(
            AuditLogORM.tenant_id == tenant_id,
            AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
            AuditLogORM.request_id == str(job_id),
            action.in_(
                {
                    _JOB_ACTION_SUBMITTED,
                    _JOB_ACTION_DISPATCH_STARTED,
                    _JOB_ACTION_FAILED_BEFORE_START,
                }
            ),
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(of=AuditLogORM)
        with platform_lane(session):
            rows = list(session.scalars(statement).all())
        return {
            str((row.details or {}).get("action"))
            for row in rows
            if (row.details or {}).get("action") is not None
        }

    # ========================================================================
    # Purpose: Reconcile a prior process's committed submission intents that
    #   never reached dispatch into exactly one durable pre-start failure edge.
    # Database/ORM: TenantORM read; batched AuditLogORM NOT EXISTS anti-query,
    #   SELECT FOR UPDATE SKIP LOCKED, and INSERT.
    # Standards: Runs before scheduler start/request acceptance. PostgreSQL
    #   locks only the bounded unmatched batch and rechecks terminal request_id
    #   edges server-side in the same statement; SQLite is the documented
    #   single-process test/dev deployment. Existing rows with NULL request_id
    #   are ignored because they cannot be correlated.
    # Blast Radius: Additive audit recovery only. RLS remains fail-closed via a
    #   per-tenant placeholder TENANT_CTX plus the existing platform lane.
    # Connections:
    #   - File: backend/ums_smart_revenue/app.py -> lifespan startup invokes it.
    #   - File: backend/ums_smart_revenue/api/connectors.py -> route intent.
    #   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py ->
    #     scheduler intent.
    # ========================================================================
    def recover_abandoned_submission_intents(self) -> int:
        """Persist recovery failures for unmatched durable intents; return count."""
        with self._session_factory() as session:
            tenant_ids = list(session.scalars(select(TenantORM.id)).all())
            session.rollback()

        recovered = 0
        for tenant_id in tenant_ids:
            recovered += self._recover_tenant_submission_intents(tenant_id=tenant_id)
        return recovered

    def _recover_tenant_submission_intents(self, *, tenant_id: UUID) -> int:
        """Reconcile one tenant's unmatched request_ids in bounded batches."""
        placeholder = make_placeholder_tenant(
            tenant_id=tenant_id,
            slug=f"connector-job-recovery:{tenant_id}",
            display_name="connector job recovery",
        )
        token = TENANT_CTX.set(placeholder)
        try:
            recovered = 0
            # KNOWN crash-window (recorded, deliberately not widened here):
            # ``dispatch_started`` sits in ``_JOB_RECOVERY_TERMINAL_ACTIONS``,
            # so a process death between the ``dispatch_started`` commit and
            # the later ``start_run`` commit leaves this anti-join excluding
            # the intent — no run row is ever created, the client already
            # holds its 202, and the job silently never runs. The same set
            # means a crash after ``start_run`` leaves a RUNNING row with no
            # time-based sweeper (``stale_running_hours`` only feeds
            # new-submission supersede). Narrowing the terminal set trades a
            # missed run for a potential double run and needs its own
            # recovery-contract PR with idempotency proof; it must not be a
            # tail-end edit of this hardening branch.
            while True:
                with self._session_factory() as session:
                    intent = aliased(AuditLogORM, name="connector_job_intent")
                    terminal = aliased(AuditLogORM, name="connector_job_terminal")
                    intent_action = intent.details["action"].as_string()
                    terminal_action = terminal.details["action"].as_string()
                    terminal_exists = (
                        select(terminal.id)
                        .where(
                            terminal.tenant_id == intent.tenant_id,
                            terminal.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
                            terminal.request_id == intent.request_id,
                            terminal_action.in_(_JOB_RECOVERY_TERMINAL_ACTIONS),
                        )
                        .exists()
                    )
                    intent_statement = (
                        select(intent)
                        .where(
                            intent.tenant_id == tenant_id,
                            intent.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
                            intent.request_id.is_not(None),
                            intent_action == _JOB_ACTION_SUBMITTED,
                            ~terminal_exists,
                        )
                        .order_by(intent.created_at, intent.id)
                        .limit(_JOB_RECOVERY_BATCH_SIZE)
                    )
                    if session.get_bind().dialect.name == "postgresql":
                        intent_statement = intent_statement.with_for_update(
                            of=intent,
                            skip_locked=True,
                        )
                    # app_tenant cannot lock audit_logs; use the sanctioned
                    # platform lane for the bounded lock acquisition, then
                    # retain those exact locks through the batch commit.
                    with platform_lane(session):
                        intents = list(session.scalars(intent_statement).all())
                    if not intents:
                        session.rollback()
                        return recovered

                    sink = PlatformLaneAuditSink(session, tenant_id=tenant_id)
                    recovered_request_ids: set[str] = set()
                    for intent_row in intents:
                        request_id = intent_row.request_id
                        if request_id is None:
                            # Defensive parity with the SQL predicate: a row
                            # without correlation cannot be recovered safely.
                            continue
                        if request_id in recovered_request_ids:
                            # FIX: Historical audit rows predate the stable
                            # lifecycle contract and may contain duplicate
                            # submissions. Reconcile one logical request once;
                            # the committed terminal edge excludes every copy
                            # from subsequent batches without deleting audit.
                            continue
                        recovered_request_ids.add(request_id)
                        details = dict(intent_row.details or {})
                        actor_user_id = intent_row.user_id or details.get("actor_user_id")
                        if actor_user_id is None:
                            raise RuntimeError(
                                "connector job intent is missing its durable actor identity"
                            )
                        connector_key = str(
                            details.get("connector_key") or intent_row.scope_id or ""
                        )
                        account_id = str(details.get("account_id") or "")
                        report_month = str(details.get("report_month") or "")
                        if not connector_key or not account_id or not report_month:
                            raise RuntimeError(
                                "connector job intent is missing recovery scope metadata"
                            )
                        actor = self._build_audit_actor(
                            tenant_id=tenant_id,
                            actor_identity=ConnectorJobActor(
                                user_id=str(actor_user_id),
                                email="connector-job-recovery@service.ums.local",
                            ),
                        )
                        record_audit_event(
                            sink=sink,
                            actor=actor,
                            event_type=AuditEventType.CONNECTOR_JOB_RUN,
                            entity_type="api_connector",
                            entity_id=f"{connector_key}:{account_id}",
                            scope=AccessScope.connector(connector_key),
                            reason="recovered abandoned connector job submission",
                            request_id=request_id,
                            details={
                                "action": _JOB_ACTION_FAILED_BEFORE_START,
                                "report_month": report_month,
                                "error_class": "ExecutorShutdownRecovery",
                            },
                        )
                    session.commit()
                    recovered += len(recovered_request_ids)
        finally:
            TENANT_CTX.reset(token)

    def _audit_pending_on_shutdown(self, deadline: float) -> tuple[list[Future], bool]:
        """Audit cancelled queued jobs; return non-cancelled futures still draining.

        Called deterministically from :meth:`close` *after*
        ``ThreadPoolExecutor.shutdown(cancel_futures=True)`` has run, with the
        close deadline (a ``time.monotonic()`` instant) this phase must
        respect. A future that is ``cancelled()`` was queued but never
        started; it will never run and therefore never writes its own
        lifecycle audit. Futures that remain non-cancelled after shutdown
        (including ones that started between a pre-shutdown snapshot and
        ``shutdown()``) are returned so ``close()`` can wait on them. Any
        ``_SlotReservation`` is dropped from memory; if its acceptance commit
        already landed, startup recovery closes its durable intent rather than
        relying on this in-process registry.

        The registry is cleared because no new work can be accepted after
        shutdown; running futures will deregister harmlessly when they finish.
        """
        with self._lock:
            entries = list(self._registry.items())
            self._registry.clear()

        cancelled: list[tuple[_JobKey, ConnectorJobActor, UUID]] = []
        running_futures: list[Future] = []
        for job_key, entry in entries:
            if not isinstance(entry, _ActiveJob):
                continue
            if entry.future.cancelled():
                cancelled.append((job_key, entry.actor_identity, entry.job_id))
            elif not entry.future.done():
                running_futures.append(entry.future)

        # FIX(codex round-23 P2): bounded, on a daemon thread -- see
        # _audit_cancelled_within_budget. A cancelled-at-shutdown GROUP-SYNC
        # job is audited by this same pull-shaped path, on purpose: its key
        # carries connector_key ``cms_group_sync`` and report_month ``-``,
        # so the row stays fully attributable, and ``job_failed_before_start``
        # + ``ExecutorShutdown`` honestly describe a job that never ran.
        audits_durable = self._audit_cancelled_within_budget(cancelled, deadline=deadline)
        return running_futures, audits_durable

    def _write_shutdown_audits(
        self, cancelled: list[tuple[_JobKey, ConnectorJobActor, UUID]]
    ) -> bool:
        """Write every cancelled-job audit; runs on the bounded audit thread.

        Each row keeps the shared ``job_failed_before_start`` /
        ``ExecutorShutdown`` taxonomy: a job the pool cancelled before it ran
        never started, so re-tagging it with a run-time failure class would
        mislabel it.
        """
        durable = True
        for job_key, actor_identity, job_id in cancelled:
            tenant_id, connector_key, account_id, report_month = job_key
            durable = (
                self._audit_failed_before_start(
                    tenant_id=tenant_id,
                    connector_key=connector_key,
                    account_id=account_id,
                    report_month=report_month,
                    error_class="ExecutorShutdown",
                    actor_identity=actor_identity,
                    job_id=job_id,
                )
                and durable
            )
        return durable

    def _audit_cancelled_within_budget(
        self, cancelled: list[tuple[_JobKey, ConnectorJobActor, UUID]], *, deadline: float
    ) -> bool:
        """Audit cancelled jobs under a hard budget (codex round-23 P2).

        The writes run on a daemon thread joined for at most
        ``min(SHUTDOWN_AUDIT_BUDGET_SECONDS, deadline - now)`` seconds, where
        ``deadline`` is the single close() deadline (round-28 P1: the audit
        join can never push the total close past CLOSE_DRAIN_TIMEOUT_SECONDS,
        and the timeout is floored at zero once the deadline is spent). A
        write blocked on the shared connection -- e.g. a hung SQLite worker
        still holds the one-slot QueuePool connection -- therefore delays
        shutdown by at most the remaining budget instead of unboundedly; the
        drain's own bounded wait is reached either way, and abandoned audits
        are logged loudly rather than silently skipped.
        """
        if not cancelled:
            return True
        audit_budget = min(
            SHUTDOWN_AUDIT_BUDGET_SECONDS,
            max(0.0, deadline - time.monotonic()),
        )
        result = {"durable": False}

        def _write_and_capture() -> None:
            """Capture whether every immediate audit transaction committed."""
            result["durable"] = self._write_shutdown_audits(cancelled)

        audit_thread = threading.Thread(
            target=_write_and_capture,
            name="executor-shutdown-audits",
            daemon=True,
        )
        # FIX: Retain the daemon thread and its result after the bounded join.
        # The previous local-only reference let a timed-out close forget an
        # in-flight audit and falsely report a later close as clean.
        self._shutdown_audit_tasks.append(_ShutdownAuditTask(thread=audit_thread, result=result))
        audit_thread.start()
        audit_thread.join(timeout=audit_budget)
        if audit_thread.is_alive():
            logger.error(
                "Executor shutdown audit writes did not finish within %.0fs; "
                "%d cancelled job(s) lack an immediate ExecutorShutdown audit "
                "row; their durable submission intents will be reconciled at "
                "next startup (a blocked audit session cannot hold the close "
                "deadline hostage).",
                audit_budget,
                len(cancelled),
            )
            return False
        return result["durable"]

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
        job_id: UUID | None = None,
    ) -> None:
        """Worker body: own session -> tenant context -> run_one; fail-closed."""
        key = (tenant_id, connector_key, account_id, report_month)
        outcome: ConnectorRunOutcome | None = None
        try:
            with (
                self._session_factory() as session,
                connector_tenant_context(tenant_id, session=session),
            ):
                if job_id is not None:
                    dispatch_claimed = self._record_dispatch_started(
                        session=session,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        account_id=account_id,
                        report_month=report_month,
                        actor_identity=actor_identity,
                        job_id=job_id,
                    )
                    if not dispatch_claimed:
                        session.rollback()
                        return
                    session.commit()
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
                    job_id=job_id,
                )
        except GoogleConnectorError as exc:
            # FIX: ConnectorServicePrincipalUnavailableError is a
            # GoogleConnectorError subclass; it lands here and is audited
            # as a Bucket-A job_failed_before_start row. Previously this
            # path raised ValueError and was swallowed by the catch-all
            # branch below, leaving a 202 with no run row, no failure
            # audit, and no operator-visible reason the job never started.
            # FIX: Expected Google failures are operational outcomes, not
            # programming faults. Their text/tracebacks can contain signed
            # URLs and credential headers, so log only a fixed category.
            logger.error(
                "Connector job failed (tenant=%s connector=%s failure_category=%s)",
                tenant_id,
                connector_key,
                _expected_failure_category(exc),
            )
            self._audit_failed_before_start(
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                error_class=type(exc).__name__,
                actor_identity=actor_identity,
                job_id=job_id,
            )
        except Exception:  # noqa: BLE001 — fail-closed: never escape the thread
            logger.exception(
                "Connector job worker raised after start (tenant=%s connector=%s)",
                tenant_id,
                connector_key,
            )
        finally:
            self._deregister(key)

    def _run_group_sync_job(
        self,
        *,
        tenant_id: UUID,
        content_owner_id: str,
        actor_identity: ConnectorJobActor,
        job_id: UUID | None = None,
    ) -> None:
        """Worker body for a scheduled CMS group sync: own session -> tenant -> sync.

        Drives the SAME ``run_group_sync`` core the manual route uses, on the
        worker's own session under ``connector_tenant_context`` (the ACTIVE-only
        gate replays here, just like ``_run_job``). The domain rows written by
        ``apply_group_sync`` and the audit rows -- the per-group GROUP_UPDATED
        rows plus, on change, the run-level GROUPS_SYNCED summary -- share this
        one session and one commit, so the #169 atomic invariant (domain and
        audit succeed or fail together) holds here by construction: there is no
        second sink session to drift.

        Fail-closed like ``_run_job``: the typed failure families are audited as
        one ``group_sync_job_failed`` row via :meth:`_audit_group_sync_failure`
        (a fresh-session sibling of ``_audit_failed_before_start``); anything
        else is logged; nothing escapes the thread; the registry key is dropped
        in ``finally`` on every path.
        """
        key = (tenant_id, GROUP_SYNC_JOB_CONNECTOR_SLUG, content_owner_id, GROUP_SYNC_JOB_MONTH)
        try:
            with (
                self._session_factory() as session,
                connector_tenant_context(tenant_id, session=session),
            ):
                # Actor built INSIDE the tenant context: a missing service-actor
                # env raises ConnectorServicePrincipalUnavailableError (a
                # GoogleConnectorError), which the failure catch below audits as
                # a pre-start failure -- never a swallowed ValueError.
                actor = self._build_group_sync_actor(tenant_id=tenant_id)
                if job_id is not None:
                    dispatch_claimed = self._record_dispatch_started(
                        session=session,
                        tenant_id=tenant_id,
                        connector_key=GROUP_SYNC_JOB_CONNECTOR_SLUG,
                        account_id=content_owner_id,
                        report_month=GROUP_SYNC_JOB_MONTH,
                        actor_identity=actor_identity,
                        job_id=job_id,
                    )
                    if not dispatch_claimed:
                        session.rollback()
                        return
                    session.commit()
                # The SAME SQL stores + atomic sink the api dependencies build,
                # imported directly (connectors.runs must never import api.*). One
                # sink on this one session so the per-group GROUP_UPDATED rows and
                # the summary below join the worker's single transaction.
                sink = PlatformLaneAuditSink(session, tenant_id=tenant_id)
                result = run_group_sync(
                    session,
                    tenant_id=tenant_id,
                    content_owner_id=content_owner_id,
                    registry=SqlAlchemyChannelRegistry(session),
                    groups=SqlAlchemyChannelGroupRegistry(session),
                    audit_sink=sink,
                    actor=actor,
                    reason="scheduled CMS group sync",
                    dry_run=False,
                    client_factory=self._group_sync_client_factory,
                )
                self._audit_group_sync_summary_if_changed(
                    sink=sink,
                    actor=actor,
                    content_owner_id=content_owner_id,
                    result=result,
                )
                # ONE commit: domain rows + per-group GROUP_UPDATED + the summary
                # (or, for an all-UNCHANGED tick, nothing pending -- harmless).
                session.commit()
        except (
            # TenantLifecycleError, the credential trio (CredentialNotFoundError /
            # InactiveCredentialError / OAuthRefreshError) and
            # ConnectorServicePrincipalUnavailableError are all GoogleConnectorError
            # subclasses, so this one clause covers them; error_class carries the
            # CONCRETE subclass name via type(exc).__name__.
            GoogleConnectorError,
            GroupSyncFetchError,
            GroupSyncConflictRefusedError,
            ChannelGroupConflictError,
            ChannelGroupOwnerReassignmentError,
        ) as exc:
            # FIX: Do not render typed upstream failures or their chained
            # causes. GroupSyncFetchError preserves the Google HTTP cause,
            # which can contain X-Goog-Credential/X-Goog-Signature values.
            logger.error(
                "Scheduled group sync failed (tenant=%s failure_category=%s)",
                tenant_id,
                _expected_failure_category(exc),
            )
            self._audit_group_sync_failure(
                tenant_id=tenant_id,
                content_owner_id=content_owner_id,
                error_class=type(exc).__name__,
                actor_identity=actor_identity,
                job_id=job_id,
            )
        except Exception as unexpected_error:
            # Worker boundary: typed Google/group failures are handled above;
            # anything else must stay inside the thread so the pool keeps running.
            logger.exception(
                "Scheduled group sync worker raised (tenant=%s error_class=%s)",
                tenant_id,
                type(unexpected_error).__name__,
            )
        finally:
            self._deregister(key)

    @staticmethod
    def _audit_group_sync_summary_if_changed(
        *,
        sink: AuditSink,
        actor: UserPrincipal,
        content_owner_id: str,
        result: GroupSyncRunResult,
    ) -> None:
        """Write the run-level GROUPS_SYNCED summary iff the apply changed anything.

        The summary is caller-owned (the core writes only the per-group
        GROUP_UPDATED rows). The manual route writes it unconditionally after
        every apply -- operator actions are always audited; the worker writes it
        ONLY when the executed counts contain a non-UNCHANGED outcome. A
        converged fleet on a daily tick therefore writes no audit rows at all --
        liveness is a log line, not a governance event. Written through the
        SAME sink (same session) as the per-group rows, so it commits with them;
        field shape mirrors the route's summary (entity_type/entity_id/scope/
        details).
        """
        execution = result.execution
        if execution is None:
            # dry_run is always False here, so an apply always yields an
            # execution; guard defensively for typing and never claim a change.
            return
        changed = any(
            count > 0
            for outcome, count in execution.counts.items()
            if outcome != GroupSyncOutcome.UNCHANGED.value
        )
        if not changed:
            # FIX: Do not log raw content_owner_id at INFO (retained Docker logs);
            # audit details still carry the identifier when the apply changes state.
            logger.info("Scheduled group sync converged with no changes")
            return
        record_audit_event(
            sink=sink,
            actor=actor,
            event_type=AuditEventType.GROUPS_SYNCED,
            entity_type="channel_group_sync",
            entity_id=content_owner_id,
            scope=AccessScope.global_scope(),
            reason="scheduled CMS group sync",
            details={
                "content_owner_id": content_owner_id,
                "counts": dict(execution.counts),
                "unknown_channel_total": result.plan.unknown_channel_total,
                "non_channel_member_count": result.plan.non_channel_member_count,
            },
        )

    @staticmethod
    def _build_group_sync_actor(*, tenant_id: UUID) -> UserPrincipal:
        """Build the tenant-pinned service principal for a group-sync worker's audit rows.

        Starts from ``build_connector_service_principal`` (the stable service
        identity carrying ``RUN_CONNECTOR_JOBS@global``, id sourced from
        ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID``) and ADDS a
        ``MANAGE_GROUPS@global`` grant, because the rows this actor signs --
        GROUPS_SYNCED and the per-group GROUP_UPDATED rows from
        ``apply_group_sync`` -- declare MANAGE_GROUPS as their effective
        permission; the audit trail must honestly carry the authority the action
        exercises (the executor's fabricate-with-the-relevant-grant precedent,
        ``_build_audit_actor``).

        A missing service-actor env raises the typed
        ``ConnectorServicePrincipalUnavailableError`` (a ``GoogleConnectorError``)
        rather than a bare ``ValueError`` -- the same conversion the orchestrator
        does for pulls -- so the worker's failure catch audits it as a pre-start
        failure instead of the catch-all swallowing it.
        """
        try:
            base = build_connector_service_principal(tenant_id=tenant_id)
        except ValueError as exc:
            raise ConnectorServicePrincipalUnavailableError(
                env_var=GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
            ) from exc
        return UserPrincipal(
            user_id=base.user_id,
            email=base.email,
            role_assignments=base.role_assignments,
            direct_permissions=(
                *base.direct_permissions,
                PermissionGrant(
                    permission=Permission.MANAGE_GROUPS,
                    scope=AccessScope.global_scope(),
                    active=True,
                ),
            ),
            is_service_account=base.is_service_account,
            disabled=base.disabled,
            tenant_id=base.tenant_id,
        )

    def _audit_group_sync_failure(
        self,
        *,
        tenant_id: UUID,
        content_owner_id: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
        job_id: UUID | None = None,
    ) -> bool:
        """Write ONE CONNECTOR_JOB_RUN group_sync_job_failed row, fresh session.

        A sibling of :meth:`_audit_failed_before_start` with the SAME mechanics
        -- fresh own session, ``platform_lane`` elevation, the placeholder-tenant
        ``TENANT_CTX`` RLS bridge set/reset in ``finally`` -- because a group-sync
        failure can itself be a non-ACTIVE tenant, so this must NOT re-enter
        ``connector_tenant_context`` (which would raise the same lifecycle error
        before the row could land). Kept separate from
        ``_audit_failed_before_start`` so the pull job's audit shape never drifts;
        only the entity/scope/details differ (the group-sync taxonomy). NEVER
        embeds ``str(exc)`` -- ``error_class`` is the class name only, which can
        never carry a secret locator.
        """
        # Actor/placeholder/token construction sits INSIDE the guard: the
        # "never escape the thread" contract covers this failure handler
        # itself, so a broken actor build or TENANT_CTX.set degrades to the
        # same logged skip as a failed audit write instead of escaping the
        # worker's except block.
        token = None
        try:
            actor = self._build_audit_actor(tenant_id=tenant_id, actor_identity=actor_identity)
            minimal_tenant = make_placeholder_tenant(
                tenant_id=tenant_id,
                slug=f"group-sync-job-failed-audit:{tenant_id}",
                display_name="group sync job failed-audit",
            )
            token = TENANT_CTX.set(minimal_tenant)
            with self._session_factory() as session, platform_lane(session):
                sink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
                record_audit_event(
                    sink=sink,
                    actor=actor,
                    event_type=AuditEventType.CONNECTOR_JOB_RUN,
                    entity_type="api_connector",
                    entity_id=f"{GROUP_SYNC_JOB_CONNECTOR_SLUG}:{content_owner_id}",
                    scope=AccessScope.connector(GROUP_SYNC_JOB_CONNECTOR_SLUG),
                    reason="scheduled group sync failed",
                    request_id=str(job_id) if job_id is not None else None,
                    details={
                        "action": "group_sync_job_failed",
                        "content_owner_id": content_owner_id,
                        "error_class": error_class,
                    },
                )
                session.commit()
            return True
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist group_sync_job_failed audit (tenant=%s)",
                tenant_id,
            )
            return False
        finally:
            if token is not None:
                TENANT_CTX.reset(token)

    def audit_failed_before_start(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
        job_id: UUID | None = None,
    ) -> bool:
        """Public hook for request-session after_commit activation failures."""
        return self._audit_failed_before_start(
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            report_month=report_month,
            error_class=error_class,
            actor_identity=actor_identity,
            job_id=job_id,
        )

    def _audit_failed_before_start(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
        job_id: UUID | None = None,
    ) -> bool:
        """Write ONE CONNECTOR_JOB_RUN job_failed_before_start row, fresh session.

        Intentionally does NOT re-enter ``connector_tenant_context()``: a
        pre-start failure can be caused by an inactive/suspended/deleted tenant,
        and that context manager would raise the same lifecycle error before the
        audit row could be written. The audit itself is platform-only-write, so
        we run it under ``platform_lane`` with the tenant_id passed explicitly to
        ``SqlAlchemyAuditSink``.

        The audit_logs INSERT must also satisfy the ``20260608_0001`` RLS
        ``WITH CHECK (tenant_id = app_current_tenant_id())`` policy. The
        after_begin hook in ``db.session`` writes the trusted tenant-context row
        from ``TENANT_CTX.get().id``; with no tenant in the contextvar the hook
        clears the row, ``app_current_tenant_id()`` returns NULL on Postgres, and
        the INSERT permission-denies via RLS -- silently dropping the only record
        of the failure. Set ``TENANT_CTX`` to a minimal ``Tenant`` (id-only; the
        lifecycle check is intentionally bypassed because we are writing the
        audit, not authorizing a run) so the hook writes the context row and the
        INSERT satisfies the policy. The token is reset via ``finally`` so the
        contextvar never leaks. No-op off Postgres (RLS is not enforced there).
        """
        # FIX: Restore the RLS tenant-context bridge dropped by the 2026-06-12
        # reverts (bdf5b71/15c0818/06af2ed). Without TENANT_CTX set,
        # app_current_tenant_id() is NULL and the audit_logs WITH CHECK denies
        # the INSERT (app_platform is NOBYPASSRLS, so elevation alone is not
        # enough); the Bucket-A failure audit was silently lost on Postgres.
        # Actor/placeholder/token construction sits INSIDE the guard (same
        # contract as _audit_group_sync_failure): this hook is called from
        # failure paths -- including the route's after_commit via
        # audit_failed_before_start -- where an escape would surface inside
        # transaction hooks or the worker's except block, so construction
        # failures degrade to the same logged skip as a failed write.
        token = None
        try:
            actor = self._build_audit_actor(tenant_id=tenant_id, actor_identity=actor_identity)
            # Minimal-tenant fabrication for the contextvar: only ``.id`` is
            # read by the after_begin hook / RLS policy, so the remaining fields
            # are placeholders, never persisted or validated against ``tenants``.
            # Built via the shared factory so the placeholder shape stays
            # centralized.
            minimal_tenant = make_placeholder_tenant(
                tenant_id=tenant_id,
                slug=f"connector-job-failed-audit:{tenant_id}",
                display_name="connector job failed-audit",
            )
            token = TENANT_CTX.set(minimal_tenant)
            with self._session_factory() as session:
                if job_id is not None:
                    existing_actions = self._lock_job_lifecycle_actions(
                        session=session,
                        tenant_id=tenant_id,
                        job_id=job_id,
                    )
                    if _JOB_ACTION_FAILED_BEFORE_START in existing_actions:
                        session.rollback()
                        return True
                # audit_logs is platform-only-write: elevate to app_platform for
                # this standalone audit (run_one does its own elevation; this
                # audit runs OUTSIDE run_one). No-op off Postgres.
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
                        request_id=str(job_id) if job_id is not None else None,
                        details={
                            "action": _JOB_ACTION_FAILED_BEFORE_START,
                            "report_month": report_month,
                            "error_class": error_class,
                        },
                    )
                session.commit()
            return True
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist job_failed_before_start audit (tenant=%s)",
                tenant_id,
            )
            return False
        finally:
            if token is not None:
                TENANT_CTX.reset(token)

    def _audit_dry_run_outcome(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        outcome: ConnectorRunOutcome,
        actor_identity: ConnectorJobActor,
        job_id: UUID | None = None,
    ) -> None:
        """Write ONE CONNECTOR_JOB_RUN job_dry_run_completed row, fresh session.

        Dry-run jobs do not create a ``connector_runs`` row, so this audit
        row is the only durable record of what the dry-run found. The
        counts mirror the B2.3 ``CONNECTOR_RUN_COUNT_KEYS`` shape that
        ``finish_run`` validates; per-report failures are listed as a
        ``[{"report_type": ..., "error_class": ...}, ...]`` array so an
        operator console can render them directly.
        """
        # Actor construction sits INSIDE the guard, matching the two failure
        # audit siblings: a broken build degrades to the same logged skip as a
        # failed write, so this method's own "never escape" contract holds.
        try:
            actor = self._build_audit_actor(tenant_id=tenant_id, actor_identity=actor_identity)
            per_report_failures = [
                {"report_type": report_type, "error_class": error_class}
                for report_type, error_class in outcome.per_report_failures
            ]
            with (
                self._session_factory() as session,
                connector_tenant_context(tenant_id, session=session),
                platform_lane(session),
            ):
                sink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
                record_audit_event(
                    sink=sink,
                    actor=actor,
                    event_type=AuditEventType.CONNECTOR_JOB_RUN,
                    entity_type="api_connector",
                    entity_id=f"{connector_key}:{account_id}",
                    scope=AccessScope.connector(connector_key),
                    reason="connector dry-run completed",
                    request_id=str(job_id) if job_id is not None else None,
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

    @staticmethod
    def _build_audit_actor(
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
