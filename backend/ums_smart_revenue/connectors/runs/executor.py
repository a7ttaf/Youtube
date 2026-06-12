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
from ums_smart_revenue.connectors.runs.orchestrator import run_one
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


# ============================================================================
# Purpose: Own a bounded ThreadPoolExecutor + an in-process registry of live
#   jobs keyed (tenant, connector_key, account_id, report_month), and run each
#   submitted connector pull on its OWN session under connector_tenant_context
#   (re-establishing TENANT_CTX in the worker thread, which does not inherit the
#   request contextvar). Mirrors the TenantResolverMiddleware executor pattern
#   (weakref.finalize GC backstop + explicit close()).
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
#   job_failed_before_start), connector run lifecycle. No finance math change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> executor +
#     weakref.finalize + close() precedent.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     connector_tenant_context replays the ACTIVE-only tenant gate.
#   - File: scripts/run_google_connector.py -> the CLI pattern this reuses.
# ============================================================================
class ConnectorJobExecutor:
    """Bounded in-process runner for connector pull jobs with a dup registry."""

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
        self._registry: dict[_JobKey, Future] = {}
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
        """Return whether a live Future exists for the exact scope (under lock)."""
        key = (tenant_id, connector_key, account_id, report_month)
        with self._lock:
            return key in self._registry

    def _register(self, key: _JobKey) -> None:
        """Reserve a registry slot before submission (caller holds no lock)."""
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
        """Register the scope and submit the pull to the worker pool."""
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
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    run_one(
                        session,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        account_id=account_id,
                        report_month=report_month,
                        dry_run=dry_run,
                        triggered_by_user_id=triggered_by_user_id,
                    )
        except GoogleConnectorError as exc:
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
        actor = UserPrincipal(
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
