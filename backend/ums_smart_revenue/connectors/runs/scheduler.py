# ============================================================================
# Purpose: Tick a daemon thread on a fixed interval and, for every ACTIVE
#   tenant's active youtube-analytics connector credential, submit a CMS
#   group-sync job to the ConnectorJobExecutor (Sched 2's
#   submit_group_sync_if_absent -> activate flow) so grouping converges
#   automatically without an operator curling POST /channels/groups/sync. The
#   active-credential list on a tenant IS the subscription list -- a tenant
#   opts a content owner into scheduled sync by registering its credential,
#   and revoking the credential unsubscribes it. No new table, no new HTTP
#   surface; this scheduler is the only submitter of this job kind.
# Database/ORM: opens its own Session per tick via session_factory; reads
#   TenantORM (db/tenant_models.py, deliberately unscoped -- see Standards)
#   and, per tenant under connector_tenant_context, api_connector_credentials
#   via SqlAlchemyConnectorCredentialRepository.list_credentials (paged).
#   Writes NOTHING itself: submission is an in-memory registry reservation on
#   the executor; the worker (executor.py::_run_group_sync_job) owns every
#   domain and audit row for the job it runs.
# Standards: `tenants` is deliberately OUTSIDE TENANT_SCOPED_TABLES (db/rls.py's
#   own guard query excludes it: "table_name <> 'tenants'"), so the enumeration
#   query needs no tenant context and no RLS policy applies to it either way.
#   Every per-tenant credential read DOES need one, taken via
#   connector_tenant_context -- one `with` block per tenant, never nested.
#   tick() shares ONE session across the whole tick (unlike the executor's
#   one-session-per-job workers), so it rolls that session back explicitly
#   after the enumeration query AND after each tenant's block. Without that,
#   a still-open transaction from an earlier read makes
#   connector_tenant_context's own RLS-boundary fix skip resetting the
#   Postgres role/tenant pin for the NEXT tenant (that fix only resets the
#   transaction when ITS lookup is the session's first statement -- see
#   tenant_context.py's docstring), and every tenant after the first would
#   silently read zero credentials under RLS on Postgres while still passing
#   on SQLite (RLS is not enforced there), so this would sail through a
#   SQLite-only test suite undetected. Fault isolation is per-tenant: one
#   tenant's failure (non-ACTIVE at read time, a DB hiccup, anything) is
#   logged and skipped inside tick() itself; a failure in the enumeration
#   sliver before the per-tenant loop even starts is NOT caught here -- that
#   is _tick_safely's job one level up, so tick() stays directly testable for
#   its per-tenant behavior. Activation is immediate, no after-commit dance:
#   the scheduler writes no submission audit row of its own to defer behind --
#   audit rows are governance events the WORKER owns, not a heartbeat for the
#   submitter.
# Blast Radius: Triggers CMS group-sync jobs on a timer. No direct writes; the
#   jobs it submits run through the SAME core (group_sync.py) and worker
#   (executor.py) the manual route and Sched 2 already exercise, so their
#   audit/RLS/atomicity guarantees are inherited here, not reimplemented.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     submit_group_sync_if_absent / activate; the weakref.finalize + close()
#     shutdown precedent this module mirrors.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     connector_tenant_context; its docstring documents the RLS-boundary
#     hazard this module's per-tenant rollback exists to avoid.
#   - File: backend/ums_smart_revenue/connectors/credentials.py ->
#     SqlAlchemyConnectorCredentialRepository.list_credentials (paged).
#   - File: backend/ums_smart_revenue/db/rls.py -> TENANT_SCOPED_TABLES; the
#     `tenants` exemption this module's enumeration query relies on.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py ->
#     TenantResolverMiddleware; the weakref.finalize GC-backstop pattern.
# ============================================================================
"""In-process scheduler that ticks CMS group-sync submissions for ACTIVE tenants."""

from __future__ import annotations

import hashlib
import logging
import threading
import weakref
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.credentials import (
    MAX_CREDENTIAL_PAGE_SIZE,
    SqlAlchemyConnectorCredentialRepository,
)
from ums_smart_revenue.connectors.google.audit import _SERVICE_ACCOUNT_EMAIL
from ums_smart_revenue.connectors.keys import YOUTUBE_ANALYTICS_CONNECTOR
from ums_smart_revenue.connectors.runs.executor import ConnectorJobActor, ConnectorJobExecutor
from ums_smart_revenue.connectors.runs.tenant_context import connector_tenant_context
from ums_smart_revenue.db.session import SessionFactory
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.tenancy.models import TenantStatus

logger = logging.getLogger(__name__)

# The largest legal page (MAX_CREDENTIAL_PAGE_SIZE) is the production default --
# it minimizes round trips for the common case of a handful of active
# youtube-analytics credentials per tenant. Tests monkeypatch this module
# attribute down (e.g. to 1, list_credentials' own floor) to force multi-page
# traversal with only a couple of seeded rows.
_CREDENTIAL_LIST_PAGE_SIZE: int = MAX_CREDENTIAL_PAGE_SIZE

# connectors/credentials.py (derive_credential_health_state, ~line 112;
# live_credential_rejection_detail, ~line 146) and
# connectors/runs/orchestrator.py::resolve_connector_credentials (~line 705)
# all gate on this exact literal -- grepped before writing this module, and
# none of the three exports a shared constant to import. Mirror the SAME
# literal here rather than inventing a symbol that could silently drift from
# theirs (see the Sched 3 task report for this verification).
_CREDENTIAL_STATUS_ACTIVE = "active"

# Bounded join timeout for close(): generous enough for an in-flight tick's
# per-tenant DB reads to unwind, short enough that a wedged thread cannot hang
# app shutdown forever.
_CLOSE_JOIN_TIMEOUT_SECONDS = 10.0


# ============================================================================
# Purpose: Daemon-thread scheduler that ticks once per interval and submits a
#   CMS group-sync reservation for every ACTIVE tenant's active
#   youtube-analytics credential, so grouping converges without an operator
#   curling the manual sync route. The full design contract (tenants
#   exemption, shared-session rollback discipline, fault isolation) sits in
#   the module contract block at the top of this file.
# Database/ORM: reads TenantORM + api_connector_credentials through one
#   shared, explicitly-rolled-back session per tick; writes NOTHING itself --
#   the executor worker it activates owns every domain and audit row.
# Standards: explicit start()/close() with a weakref.finalize GC backstop;
#   per-tenant fault isolation inside tick(); the stop flag is checked per
#   tenant and per submission so close() halts NEW submissions promptly;
#   _tick_safely never lets a poisoned tick kill the thread.
# Blast Radius: Triggers CMS group-sync jobs on a timer; the jobs inherit the
#   audit/RLS/atomicity guarantees of the shared group_sync.py core.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     submit_group_sync_if_absent / activate / cancel_reservation.
#   - File: backend/ums_smart_revenue/app.py -> boot wiring + lifespan close.
# ============================================================================


def _owner_log_label(account_id: str) -> str:
    """Return a non-reversible fingerprint of a CMS content-owner id for logs."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]


class GroupSyncScheduler:
    """Tick a background thread that submits CMS group-sync jobs for ACTIVE tenants.

    Mirrors the ``ConnectorJobExecutor`` / ``TenantResolverMiddleware`` shutdown
    precedent: a daemon thread the caller starts explicitly via :meth:`start`,
    an explicit :meth:`close` for deterministic lifespan teardown, and a
    ``weakref.finalize`` GC backstop for any path that bypasses ``close()``.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        executor: ConnectorJobExecutor,
        interval_seconds: float,
        service_actor_id: str,
    ) -> None:
        """Store dependencies and the fixed service-principal identity for every submission.

        ``service_actor_id`` is passed IN by the caller (boot wiring reads it
        from settings) rather than read here via ``load_app_settings()`` -- the
        same seam the executor uses for its own config, kept so this class
        stays unit-testable without an environment variable and never grows an
        implicit settings dependency of its own.
        """
        self._session_factory = session_factory
        self._executor = executor
        self._interval_seconds = interval_seconds
        self._service_actor_id = service_actor_id
        # Built once: the identity is fixed for the scheduler's lifetime and
        # ConnectorJobActor is frozen, so there is no reason to reconstruct it
        # per credential entry.
        self._actor_identity = ConnectorJobActor(
            user_id=service_actor_id, email=_SERVICE_ACCOUNT_EMAIL
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # GC backstop only -- NOT a substitute for close(). Setting the stop
        # flag is enough to let an abandoned thread wake from its current
        # Event.wait() and exit on its own. The finalizer deliberately does
        # not join (finalizers must not block) and does not reference `self`
        # (it is bound to `self._stop`, not `self`, so it cannot itself keep
        # this object alive and fail to ever fire).
        # FIX: the backstop was previously defeated by the tick thread itself
        # -- its `target=self._run_loop` bound method strongly retained this
        # scheduler, so an abandoned RUNNING scheduler could never be
        # collected and the finalizer never fired. start() now uses a detached
        # loop closure holding only a weakref + the stop event; see its note.
        self._finalizer = weakref.finalize(self, self._stop.set)

    def start(self) -> None:
        """Spawn the daemon tick thread.

        A second call is a harmless no-op (the first thread is left running);
        boot wiring calls this exactly once. Not started automatically in
        ``__init__`` because most tests drive :meth:`tick` directly and never
        want a background thread at all. The FIRST tick fires one full
        interval after start(), not immediately -- ``Event.wait(interval)``
        blocks for the interval (or returns early+True the instant close()
        sets the stop flag) before the loop body ever runs, so a deploy
        restart does not thunder every ACTIVE tenant's group sync the moment
        the process comes back up.
        """
        if self._thread is not None:
            return
        # The thread target is deliberately NOT a bound method on self: a
        # bound target strongly retains this scheduler for the thread's whole
        # lifetime, which made the weakref.finalize GC backstop above
        # un-fireable for an abandoned RUNNING scheduler -- the exact case it
        # exists for. The loop closure captures only the stop event, the
        # interval, a weakref, and the UNBOUND _tick_safely function. Its
        # per-tick strong reference is scoped to the _tick_once frame (dying
        # on return -- no `del`), so during the long Event.wait() the loop
        # holds nothing strong: an abandoned scheduler really is collected,
        # the finalizer sets the stop event, and the loop exits on its next
        # wake.
        stop = self._stop
        interval_seconds = self._interval_seconds
        self_ref = weakref.ref(self)
        tick_safely = GroupSyncScheduler._tick_safely

        def _tick_once() -> bool:
            """Tick iff the scheduler still lives; False means collected -- stop looping."""
            scheduler = self_ref()
            if scheduler is None:
                # The abandoned scheduler was collected and the finalizer
                # already set the stop event; nothing left to tick for.
                return False
            tick_safely(scheduler)
            return True

        def _detached_loop() -> None:
            while not stop.wait(interval_seconds):
                if not _tick_once():
                    return

        self._thread = threading.Thread(
            target=_detached_loop,
            name="ums-group-sync-scheduler",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the tick thread deterministically: set the stop flag, then join.

        Safe to call multiple times and safe to call on a scheduler that was
        never started (``_thread`` stays ``None``; the join is skipped).
        ``Event.set()`` wakes a thread blocked in ``Event.wait(interval)``
        immediately, so the join below is prompt even with a long interval --
        it does not wait for the next scheduled tick.

        A thread still alive after the bounded join is winding down, not
        starting new work -- ``tick`` checks the stop flag per tenant and per
        submission -- but the app lifespan closes the executor right behind
        us, so that case is logged as a warning rather than silently racing
        executor shutdown.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_CLOSE_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                logger.warning(
                    "group-sync scheduler thread still running after %.1fs join;"
                    " shutdown proceeds (stop flag blocks new submissions)",
                    _CLOSE_JOIN_TIMEOUT_SECONDS,
                )
        self._finalizer.detach()

    def _tick_safely(self) -> None:
        """Catch-all wrapper around one tick: log and move on, never die.

        A tick can fail before it ever reaches per-tenant fault isolation --
        e.g. ``session_factory()`` itself raising because the DB is
        unavailable -- and :meth:`tick` does not guard that outer sliver
        itself (tests drive ``tick()`` directly to assert the per-tenant
        behavior without this wrapper masking a real bug there). This is the
        ONE place that must never let a tick kill the thread; the next tick
        still fires on the next interval regardless of this one's outcome.
        """
        try:
            self.tick()
        except Exception:  # noqa: BLE001 — fail-closed: the loop must survive
            logger.exception("group-sync scheduler tick failed; retrying next interval")

    def tick(self) -> None:
        """Submit a group-sync job for every ACTIVE tenant's active youtube-analytics credential.

        Public and synchronous so tests can drive it directly with no clock
        and no thread. Enumerates ACTIVE tenants with a plain query --
        ``tenants`` is deliberately outside ``TENANT_SCOPED_TABLES`` (see the
        module contract block above), so this read needs no tenant context.
        Each tenant's credential read DOES need one, taken via
        ``connector_tenant_context`` -- and because this method shares ONE
        session across every tenant, the session is rolled back explicitly
        after the enumeration query and after each tenant's block (see the
        module contract block for why this is load-bearing on Postgres, not
        cosmetic). Fault isolation is per-tenant: one tenant's failure is
        logged and skipped; the rest of the fleet still ticks.
        """
        with self._session_factory() as session:
            tenant_ids = self._enumerate_active_tenant_ids(session)

            submitted_total = 0
            in_flight_total = 0
            for tenant_id in tenant_ids:
                if self._stop.is_set():
                    # close() began mid-tick: stop making NEW submissions now
                    # -- the executor may be shutting down right behind us.
                    logger.info("group-sync tick aborted early: scheduler is closing")
                    break
                try:
                    with connector_tenant_context(tenant_id, session=session):
                        submitted, in_flight = self._submit_for_tenant(session, tenant_id=tenant_id)
                    submitted_total += submitted
                    in_flight_total += in_flight
                except Exception:  # noqa: BLE001 — one tenant must not starve the rest
                    logger.exception("group-sync tick: tenant %s failed; skipping", tenant_id)
                finally:
                    # Mirrors the enumeration rollback below, per tenant: clear
                    # whatever transaction this tenant's reads left open so
                    # the NEXT tenant's connector_tenant_context call again
                    # sees a transaction-free session (see the module
                    # contract block's Standards note).
                    if session.in_transaction():
                        session.rollback()

            logger.info(
                "group-sync tick: %d submitted, %d in-flight-skipped across %d tenants",
                submitted_total,
                in_flight_total,
                len(tenant_ids),
            )

    @staticmethod
    def _enumerate_active_tenant_ids(session: Session) -> list[UUID]:
        """Return every ACTIVE tenant id and leave the session transaction-free.

        Ids are extracted into a plain list BEFORE the rollback below so the
        caller never touches an expired ORM attribute afterward --
        ``Session.rollback()`` expires every object it loaded, and re-reading
        ``TenantORM.id`` after that would silently issue a fresh SELECT under
        whatever tenant context happens to be pinned at that later point in
        the per-tenant loop.
        """
        rows = session.scalars(
            select(TenantORM).where(TenantORM.status == TenantStatus.ACTIVE.value)
        ).all()
        tenant_ids = [row.id for row in rows]
        if session.in_transaction():
            session.rollback()
        return tenant_ids

    def _submit_for_tenant(self, session: Session, *, tenant_id: UUID) -> tuple[int, int]:
        """Page through one tenant's active youtube-analytics credentials and submit each.

        Returns ``(submitted_count, in_flight_skipped_count)``. Must be called
        from inside the caller's ``connector_tenant_context`` block -- the
        credential read is tenant-scoped and RLS-enforced on Postgres.
        """
        repository = SqlAlchemyConnectorCredentialRepository(session, tenant_id=tenant_id)
        submitted = 0
        in_flight_skipped = 0
        offset = 0
        while True:
            if self._stop.is_set():
                # close() began mid-tenant: return what we have; the tick's
                # per-tenant check stops the loop above us.
                return submitted, in_flight_skipped
            page = repository.list_credentials(
                connector_keys=frozenset({YOUTUBE_ANALYTICS_CONNECTOR}),
                limit=_CREDENTIAL_LIST_PAGE_SIZE,
                offset=offset,
            )
            for entry in page.items:
                if self._stop.is_set():
                    # Same close() race, one level finer: stop submitting
                    # mid-page instead of draining the tenant's credentials.
                    return submitted, in_flight_skipped
                if entry.status != _CREDENTIAL_STATUS_ACTIVE:
                    continue
                reservation = self._executor.submit_group_sync_if_absent(
                    tenant_id=tenant_id,
                    content_owner_id=entry.account_id,
                    actor_identity=self._actor_identity,
                )
                if reservation is None:
                    # Already in flight (a previous tick's job for the same
                    # owner -- a manual route sync holds no registry slot, so
                    # it can never be the cause here; see group_sync.py's
                    # concurrency note). Debug, not info: expected steady-
                    # state noise on a short interval, not an operator signal.
                    # FIX: the guarded CMS content-owner id went out raw; the
                    # README presents DEBUG as safe for operators, so only the
                    # SHA-256 fingerprint is logged now.
                    logger.debug(
                        "group-sync tick: sync already in flight (tenant=%s owner_fp=%s)",
                        tenant_id,
                        _owner_log_label(entry.account_id),
                    )
                    in_flight_skipped += 1
                    continue
                # Activate IMMEDIATELY, unlike the jobs route's after-commit
                # dance: the route defers enqueue behind ITS OWN audit-row
                # commit so a failed commit can cancel the reservation instead
                # of orphaning a worker. The scheduler writes no submission
                # audit of its own to defer behind -- audit rows are
                # governance events the WORKER owns, not a heartbeat for the
                # submitter -- so there is nothing to commit first.
                self._executor.activate(reservation)
                submitted += 1
            if not page.has_more:
                break
            offset += _CREDENTIAL_LIST_PAGE_SIZE
        return submitted, in_flight_skipped
