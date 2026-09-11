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
#     submit_if_absent + after_commit.activate / after_rollback.cancel flow.
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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import PlatformLaneAuditSink, SqlAlchemyAuditSink
from ums_smart_revenue.connectors.google.audit import build_connector_service_principal
from ums_smart_revenue.connectors.google.errors import (
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
from ums_smart_revenue.db.session import SessionFactory
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

# Bounded grace close() gives still-pending _SlotReservation hooks to run
# before the audit pool stops accepting submissions. A reservation that
# survives past this window belongs to a request that died before its
# post-commit hook fired; close() audits it itself as ExecutorShutdown.
_PENDING_HOOK_GRACE_SECONDS = 15.0


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
#   A dedicated single-worker ``_audit_executor`` carries the route's
#   post-commit activation-failure audits (``queue_failed_start_audit``):
#   inside ``after_commit`` the request session still holds its pooled
#   connection, so a synchronous fresh-session audit would block on
#   ``pool_timeout`` and drop the row; ``close()`` drains this worker with
#   ``wait=True`` so the audit outlives the shutdown that triggered it.
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
#     submit_if_absent + after_commit.activate / after_rollback.cancel_reservation.
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
        self._registry: dict[_JobKey, Future | _SlotReservation | _ActiveJob] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ums-connector-job",
        )
        # A dedicated single-worker pool for the route's post-commit failure
        # audits. Queueing there removes the audit's session checkout from the
        # request lifecycle: on SQLite (one-slot engine pool) a synchronous
        # audit inside after_commit would wait out pool_timeout while the
        # committing session still holds the connection, and a bare daemon
        # thread could die mid-write during the very shutdown that made
        # activate() fail. This worker is tracked and drained by close().
        self._audit_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="ums-connector-audit",
        )
        # Serialization between queue_failed_start_audit and close(): a submit
        # that entered before close() flipped _audit_accepting is guaranteed to
        # land in the pool before shutdown() is invoked, so it is drained —
        # never silently rejected mid-teardown.
        self._audit_lock = threading.Lock()
        self._audit_accepting = True
        # _committed: key -> the reservation instance whose after_commit hook
        # entered (proof the request transaction committed). Instance-keyed so
        # a stale hook's cleanup can never strip a same-key retry's mark.
        # _shutdown_audited keys: jobs the shutdown sweep (or post-close
        # fallback) already wrote a failure row for, so a late hook queue call
        # can never double-audit one job. Deliberately NOT populated on a
        # normal queued submit — the same job key may legitimately fail
        # activation again on a later attempt, and each failure needs its own
        # audit row.
        self._committed: dict[_JobKey, _SlotReservation] = {}
        self._shutdown_audited: set[_JobKey] = set()
        # Reservation admission gate: flipped to False by close() under
        # _lock BEFORE the pools stop, so a submission admitted before the
        # flip always has its post-commit path covered by the grace/drain
        # logic — and nothing new can be reserved mid-teardown.
        self._accepting_reservations = True
        # In-flight post-commit hooks: a hook that called
        # begin_post_commit but not yet end_post_commit. close() waits on
        # this AND pending reservations because activate() pops the registry
        # slot BEFORE the hook queues its failure audit — the registry alone
        # cannot see that window.
        self._inflight_hooks = 0
        self._finalizer = weakref.finalize(
            self,
            self._shutdown_pools,
            self._executor,
            self._audit_executor,
        )

    # ========================================================================
    # Purpose: Record that a reservation's request transaction committed — the
    #   after_commit hook can only run post-commit, so the mark is the proof
    #   close() uses to audit leftovers vs drop uncommitted ones.
    # Database/ORM: None — in-memory marker on the executor.
    # Standards: lock-guarded set keyed by the reservation's job key; keys are
    #   discarded on activate/cancel/queue transitions so the set stays bounded.
    # Blast Radius: Audit correctness — drives which leftover reservations get
    #   a job_failed_before_start row at shutdown.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py -> hook caller.
    # ========================================================================
    # ========================================================================
    # Purpose: Bracket a post-commit hook — begin marks the reservation
    #   committed and registers the hook as in-flight; end clears both. The
    #   pair keeps close()'s audit gate open across the whole hook, covering
    #   the window where activate() has already popped the registry slot but
    #   the failure audit has not yet been queued.
    # Database/ORM: None — in-memory lifecycle tracking only.
    # Standards: end_post_commit runs from the hook's finally so a raised
    #   hook can never wedge close(); counts stay balanced per reservation.
    # Blast Radius: Audit completeness — drives close()'s grace loop.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py -> hook caller.
    # ========================================================================
    def begin_post_commit(self, reservation: _SlotReservation) -> None:
        """Mark the reservation committed and register its hook as in-flight."""
        with self._lock:
            self._committed[reservation.key] = reservation
            self._inflight_hooks += 1

    # ========================================================================
    # Purpose: Complete a post-commit hook — drop the in-flight count and the
    #   committed mark, but ONLY the mark belonging to THIS reservation
    #   instance; a same-key retry's mark must survive a stale cleanup.
    # Database/ORM: None — in-memory lifecycle tracking under _lock.
    # Standards: called from the hook's finally so every code path balances
    #   its begin_post_commit; identity-checked removal keeps the counter and
    #   the committed map consistent under concurrent same-key submissions.
    # Blast Radius: Audit correctness — the committed map decides which
    #   leftover reservations close() audits as ExecutorShutdown.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py -> hook caller.
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
    #     begin_post_commit is the required pairing.
    # ========================================================================
    def end_post_commit(self, reservation: _SlotReservation) -> None:
        """Unregister the hook; release only this reservation's committed mark."""
        with self._lock:
            self._inflight_hooks = max(0, self._inflight_hooks - 1)
            if self._committed.get(reservation.key) is reservation:
                del self._committed[reservation.key]

    # ========================================================================
    # Purpose: Count unresolved post-commit work — _SlotReservation registry
    #   slots PLUS hooks currently inside begin/end_post_commit — so close()
    #   holds its audit gate across the whole hook, including the window where
    #   activate() already popped the slot but the failure audit is not yet
    #   queued.
    # Database/ORM: None — reads the in-memory registry + counter under _lock.
    # Standards: read-only; used by close()'s bounded grace loop.
    # Blast Radius: None detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
    #     close() grace loop + _audit_pending_on_shutdown.
    # ========================================================================
    def _outstanding_post_commit(self) -> int:
        """Count pending reservations plus in-flight post-commit hooks."""
        with self._lock:
            pending = sum(
                1
                for entry in self._registry.values()
                if isinstance(entry, _SlotReservation)
            )
            return pending + self._inflight_hooks

    # ========================================================================
    # Purpose: GC backstop — stop the worker and audit pools if close() was
    #   never invoked so neither pool keeps interpreter threads alive.
    # Database/ORM: None — thread-pool lifecycle only.
    # Standards: non-blocking shutdown (wait=False, cancel queued futures);
    #   deterministic drain semantics live in close(), not here.
    # Blast Radius: Process lifecycle; a bypassed close() can drop queued
    #   audits — exactly why close() is the real contract.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
    #     close() is the ordered drain; this is the fallback only.
    # ========================================================================
    @staticmethod
    def _shutdown_pools(
        executor: ThreadPoolExecutor,
        audit_executor: ThreadPoolExecutor,
    ) -> None:
        """GC backstop: stop both pools if ``close()`` never ran."""
        executor.shutdown(wait=False, cancel_futures=True)
        audit_executor.shutdown(wait=False)

    # ========================================================================
    # Purpose: Deterministic executor shutdown from the app lifespan — cancel
    #   queued work, audit cancelled futures, then drain the audit pool.
    # Database/ORM: audit_logs writes via _audit_pending_on_shutdown (own
    #   session) plus queued queue_failed_start_audit tasks before close
    #   returns.
    # Standards: ordered teardown — worker pool stops, pending audits write,
    #   audit pool drains with wait=True under the _audit_accepting lock; the
    #   weakref finalizer is the GC fallback only.
    # Blast Radius: Audit completeness — anything not drained here loses its
    #   job_failed_before_start row.
    # Connections:
    #   - File: backend/ums_smart_revenue/app.py -> lifespan calls close().
    #   - File: tests/connectors/runs/test_executor.py -> drain interleavings.
    # ========================================================================
    def close(self) -> None:
        """Shut the pool down deterministically (called from the app lifespan).

        First cancels all queued futures via ``shutdown(cancel_futures=True)``,
        then audits any futures that were definitively cancelled as
        ``job_failed_before_start`` with ``error_class="ExecutorShutdown"``.
        Running futures are allowed to finish and deregister themselves; they
        are never audited as pre-start failures. The weakref finalizer remains
        as a GC backstop for paths that bypass ``close()``.

        The audit pool is shut down LAST with ``wait=True`` so route-queued
        activation-failure audits — accepted jobs whose ``activate()`` failed
        during this very shutdown — are drained and committed before the
        process exits instead of dying on an untracked thread.
        """
        # Stop new reservations first — under _lock, so a submit_if_absent
        # that already entered lands before the flip and is covered by the
        # grace/drain below; anything later is refused (None -> route 409 /
        # scheduler skip) instead of reserving a slot nobody will activate.
        with self._lock:
            self._accepting_reservations = False
        self._executor.shutdown(wait=False, cancel_futures=True)
        # A request can commit and still have its after_commit hook pending
        # when shutdown begins, and activate() pops the registry slot before
        # the hook queues its failure audit — the grace wait must cover both
        # pending reservations and hooks in flight. Anything still unresolved
        # at the deadline is audited as ExecutorShutdown below.
        deadline = time.monotonic() + _PENDING_HOOK_GRACE_SECONDS
        while self._outstanding_post_commit() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._outstanding_post_commit():
            logger.error(
                "close() grace expired with %d unresolved post-commit units; "
                "auditing committed leftovers as ExecutorShutdown",
                self._outstanding_post_commit(),
            )
        self._audit_pending_on_shutdown()
        # Flip the accepting flag under the lock BEFORE shutdown() so a
        # queue_failed_start_audit call that already entered submits into a
        # live pool and is drained by wait=True — never rejected mid-teardown.
        with self._audit_lock:
            self._audit_accepting = False
        self._audit_executor.shutdown(wait=True)
        self._finalizer.detach()

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
            if not self._accepting_reservations or key in self._registry:
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
                if self._committed.get(key) is reservation:
                    del self._committed[key]
                raise
            if self._committed.get(key) is reservation:
                del self._committed[key]
            self._stash_and_register(
                future=future,
                key=key,
                actor_identity=reservation.actor_identity,
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
            job_kind=_JOB_KIND_GROUP_SYNC,
        )
        with self._lock:
            if not self._accepting_reservations or key in self._registry:
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
                if self._committed.get(reservation.key) is reservation:
                    del self._committed[reservation.key]
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
            self._stash_and_register(
                future=future,
                key=key,
                actor_identity=actor_identity,
            )
        return future

    def _stash_and_register(
        self,
        future: Future,
        key: _JobKey,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Stash shutdown-audit metadata on *future* and insert into the registry.

        Must be called while holding ``self._lock``.
        """
        self._registry[key] = _ActiveJob(future=future, actor_identity=actor_identity)

    def _audit_pending_on_shutdown(self) -> None:
        """Audit every accepted job that was cancelled before it started.

        Called deterministically from :meth:`close` *after*
        ``ThreadPoolExecutor.shutdown(cancel_futures=True)`` has run. A
        future that is ``cancelled()`` was queued but never started; it will
        never run and therefore never writes its own lifecycle audit. A
        running or completed future is left alone -- it (or its worker) owns
        the audit trail.

        A ``_SlotReservation`` still live when this runs is audited only if
        its ``after_commit`` hook already marked it committed — proof the
        request transaction committed and the client holds (or held) a 202.
        An UNMARKED leftover belongs to a transaction that is still open or
        was rolled back; auditing it would write a phantom failure row for a
        job that was never accepted, so it is dropped without a row. If a
        marked reservation's hook fires after this sweep, its queue call is
        skipped by the ``_shutdown_audited`` dedupe — one job, one failure
        row.

        The registry is cleared because no new work can be accepted after
        shutdown; running futures will deregister harmlessly when they finish.
        """
        # The registry clear, the committed check, and the audited-mark all
        # happen inside one critical section (self._lock -> self._audit_lock;
        # the queue path only ever takes _audit_lock, so there is no cycle).
        # A late after_commit queue call either ran before this (its own row
        # is the only one) or runs after (the key is already marked and the
        # call is skipped) — never two failure rows for one job.
        with self._lock:
            entries = list(self._registry.items())
            self._registry.clear()
            cancelled: list[tuple[_JobKey, ConnectorJobActor]] = []
            for job_key, entry in entries:
                if isinstance(entry, _SlotReservation):
                    if job_key in self._committed:
                        cancelled.append((job_key, entry.actor_identity))
                    continue
                if not isinstance(entry, _ActiveJob):
                    continue
                if not entry.future.cancelled():
                    continue
                cancelled.append((job_key, entry.actor_identity))
            with self._audit_lock:
                for job_key, _actor in cancelled:
                    self._shutdown_audited.add(job_key)

        for job_key, actor_identity in cancelled:
            tenant_id, connector_key, account_id, report_month = job_key
            # A cancelled-at-shutdown GROUP-SYNC job is audited by this SAME
            # pull-shaped path, on purpose. Its key carries connector_key
            # ``cms_group_sync`` and report_month ``-``, so the emitted row is
            # fully attributable to the sync job via its connector-key-bearing
            # entity_id + scope and the month sentinel; and
            # ``action="job_failed_before_start"`` + ``error_class="ExecutorShutdown"``
            # honestly describes a job the pool cancelled before it ran. It is
            # NOT a run-time failure, so re-tagging it with the sync taxonomy's
            # ``group_sync_job_failed`` (credential/fetch/conflict) would
            # mislabel a job that never started. Kind-awareness here would fork a
            # near-identical audit for zero governance gain, so the shared row
            # stays.
            self._audit_failed_before_start(
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                error_class="ExecutorShutdown",
                actor_identity=actor_identity,
            )

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
            with (
                self._session_factory() as session,
                connector_tenant_context(tenant_id, session=session),
            ):
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

    def _run_group_sync_job(
        self,
        *,
        tenant_id: UUID,
        content_owner_id: str,
        actor_identity: ConnectorJobActor,
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
            logger.exception(
                "Scheduled group sync failed (tenant=%s owner=%s)",
                tenant_id,
                content_owner_id,
            )
            self._audit_group_sync_failure(
                tenant_id=tenant_id,
                content_owner_id=content_owner_id,
                error_class=type(exc).__name__,
                actor_identity=actor_identity,
            )
        except Exception:  # noqa: BLE001 — fail-closed: never escape the thread
            logger.exception(
                "Scheduled group sync worker raised (tenant=%s owner=%s)",
                tenant_id,
                content_owner_id,
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
            logger.info(
                "Scheduled group sync converged with no changes (owner=%s)", content_owner_id
            )
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

        A missing or placeholder service-actor env raises the typed
        ``ConnectorServicePrincipalUnavailableError`` (a ``GoogleConnectorError``)
        straight from ``build_connector_service_principal`` -- no ValueError
        translation needed -- so the worker's failure catch audits it as a
        pre-start failure instead of the catch-all swallowing it.
        """
        base = build_connector_service_principal(tenant_id=tenant_id)
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
    ) -> None:
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
                    details={
                        "action": "group_sync_job_failed",
                        "content_owner_id": content_owner_id,
                        "error_class": error_class,
                    },
                )
                session.commit()
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist group_sync_job_failed audit (tenant=%s)",
                tenant_id,
            )
        finally:
            if token is not None:
                TENANT_CTX.reset(token)

    # ========================================================================
    # Purpose: Queue a route-level activation-failure audit on the tracked
    #   single-worker pool so the committing request never waits on a session
    #   checkout inside after_commit.
    # Database/ORM: audit_logs write deferred to _audit_failed_before_start on
    #   the audit worker's own session (platform_lane elevation).
    # Standards: accepting-flag + lock serialize submissions against close();
    #   submissions already claimed by the shutdown sweep are skipped; any
    #   submission that lands after the pool closed is handed to a
    #   last-chance daemon writer — never raised into the request lifecycle.
    # Blast Radius: Audit completeness for accepted connector jobs only.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py -> after_commit
    #     hook is the sole caller.
    # ========================================================================
    def queue_failed_start_audit(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Deliver a ``job_failed_before_start`` audit through the live path.

        Called from the route's ``after_commit`` hook when ``activate()``
        raises. Three distinct delivery paths, in order:

        1. While ``close()`` is still accepting, the audit is queued on the
           tracked audit worker whose session checkout happens OFF the
           request lifecycle — on SQLite the committing request session
           still holds the engine's only pooled connection inside
           ``after_commit``, so a synchronous audit would wait out
           ``pool_timeout`` and drop the row; on PostgreSQL a saturated pool
           produces the same stall under concurrent failures. ``close()``
           drains this pool with ``wait=True`` so an in-flight audit
           outlives the shutdown that triggered it.
        2. If the shutdown sweep already emitted this job's failure row, the
           call is skipped — one job, one row.
        3. After the audit pool closed (a request that committed past the
           grace window), a last-chance daemon thread runs the write itself:
           its checkout is not bound to this hook, so it simply waits for
           the request session's release and persists the row.
        """
        key = (tenant_id, connector_key, account_id, report_month)
        with self._audit_lock:
            if key in self._shutdown_audited:
                # close() already emitted this job's failure row.
                return
            if self._audit_accepting:
                try:
                    self._audit_executor.submit(
                        self._audit_failed_before_start,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        account_id=account_id,
                        report_month=report_month,
                        error_class=error_class,
                        actor_identity=actor_identity,
                    )
                except Exception:  # noqa: BLE001 — best-effort, never escape
                    logger.exception(
                        "Failed to queue job_failed_before_start audit "
                        "(tenant=%s)",
                        tenant_id,
                    )
                return
            # Pool closed mid-teardown: claim the fallback so a second call
            # for this job cannot double-write.
            self._shutdown_audited.add(key)
        # Last-chance write for a submission that arrived after the audit
        # pool closed (a request that committed past the shutdown grace
        # window). A synchronous write here cannot work: inside after_commit
        # the request session still holds its pooled connection, so a
        # same-engine checkout would stall — on SQLite's one-slot pool that
        # is a guaranteed pool_timeout, and under PostgreSQL saturation the
        # same deadlock shape. A daemon thread is not blocked inside this
        # hook: its checkout waits for the request session's imminent
        # release, then persists the row — the accepted 202 keeps its
        # lifecycle edge on every engine.
        try:
            threading.Thread(
                target=self._audit_failed_before_start,
                kwargs={
                    "tenant_id": tenant_id,
                    "connector_key": connector_key,
                    "account_id": account_id,
                    "report_month": report_month,
                    "error_class": error_class,
                    "actor_identity": actor_identity,
                },
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to spawn last-chance job_failed_before_start audit "
                "writer (tenant=%s)",
                tenant_id,
            )

    def audit_failed_before_start(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Public hook for request-session after_commit activation failures."""
        self._audit_failed_before_start(
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            report_month=report_month,
            error_class=error_class,
            actor_identity=actor_identity,
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
    ) -> None:
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
            with self._session_factory() as session, platform_lane(session):
                # audit_logs is platform-only-write: elevate to app_platform for
                # this standalone audit (run_one does its own elevation; this
                # audit runs OUTSIDE run_one). No-op off Postgres.
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
