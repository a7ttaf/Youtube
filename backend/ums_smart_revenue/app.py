# ============================================================================
# Purpose: Build the FastAPI application and wire API routers/dependencies.
# Database/ORM: SQLAlchemy session dependencies for request and platform flows.
# Standards: Factory-owned middleware/router wiring and explicit dependency
# overrides for tests and configured deployments.
# Blast Radius: HTTP routing, authentication dependencies, and app startup.
# Connections:
#   - File: backend/ums_smart_revenue/api/export_templates.py -> Router wiring.
#   - File: backend/ums_smart_revenue/config/settings.py -> Runtime settings.
#   - File: backend/ums_smart_revenue/config/logging_config.py -> The ASGI
#     lifespan owns the one-time process logging configuration (P0.6).
# ============================================================================
"""FastAPI application factory and router wiring."""

import logging
import threading
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ums_smart_revenue.api.adsense import router as adsense_router
from ums_smart_revenue.api.allocation import router as allocation_router
from ums_smart_revenue.api.audit import router as audit_router
from ums_smart_revenue.api.channel_account_links import (
    router as channel_account_links_router,
)
from ums_smart_revenue.api.channels import (
    router as channels_router,
)
from ums_smart_revenue.api.connectors import router as connectors_router
from ums_smart_revenue.api.dependencies import (
    authenticated_session_dependency,
    current_db_session,
    current_platform_db_session,
    current_principal_from_database,
    current_principal_from_headers,
    current_trusted_gateway_identity,
)
from ums_smart_revenue.api.dependencies_audit import (
    current_atomic_audit_sink,
    current_audit_sink,
    sql_atomic_audit_sink_from_session,
    sql_audit_sink_from_session,
)
from ums_smart_revenue.api.dependencies_finance import (
    current_revenue_audit_sink,
    sql_revenue_audit_sink_from_session,
)
from ums_smart_revenue.api.exchange_rates import router as exchange_rates_router
from ums_smart_revenue.api.export_templates import router as export_templates_router
from ums_smart_revenue.api.exports import router as exports_router
from ums_smart_revenue.api.finance_close import router as finance_close_router
from ums_smart_revenue.api.groups import (
    router as groups_router,
)
from ums_smart_revenue.api.org_units import router as org_units_router
from ums_smart_revenue.api.reconciliation import router as reconciliation_router
from ums_smart_revenue.api.registry_dependencies import (
    current_channel_registry,
    current_group_registry,
    sql_channel_registry_from_session,
    sql_group_registry_from_session,
)
from ums_smart_revenue.api.reports import router as reports_router
from ums_smart_revenue.api.revenue import router as revenue_router
from ums_smart_revenue.api.security import router as security_router
from ums_smart_revenue.api.session import router as session_router
from ums_smart_revenue.api.source_rows import router as source_rows_router
from ums_smart_revenue.api.tenants import router as tenants_router
from ums_smart_revenue.api.users import router as users_router
from ums_smart_revenue.config.logging_config import (
    LoggingConfiguration,
    configure_logging,
    release_logging_output,
    restore_logging,
)
from ums_smart_revenue.config.settings import (
    AUTHZ_SOURCE_DATABASE,
    AUTHZ_SOURCE_HEADERS,
    AppSettings,
    load_app_settings,
)
from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE
from ums_smart_revenue.connectors.runs.executor import ConnectorJobExecutor
from ums_smart_revenue.connectors.runs.scheduler import GroupSyncScheduler
from ums_smart_revenue.db.session import (
    SessionFactory,
    build_platform_session_factory,
    build_session_factory,
    session_dependency,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus
from ums_smart_revenue.tenancy.resolver import (
    DEFAULT_BYPASS_PATHS,
    TenantAuthorizer,
    TenantResolverMiddleware,
    _normalise_bypass_paths,
)
from ums_smart_revenue.tenancy.resolver import (
    SessionFactory as TenantSessionFactory,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Purpose: Release one lifespan's output/level ownership after bounded shutdown,
#   while retaining its independent redaction-safety lease until every
#   scheduler, executor, and retained audit thread reaches real termination.
# Database/ORM: None directly; surviving connector workers and a scheduler tick
#   may finish their already-open transactions before their wait methods return.
# Standards: The watcher is daemonized so it cannot extend the container's
#   bounded stop window. A failed completion observation retains safety
#   fail-closed; it is never interpreted as proof that the workers terminated.
# Blast Radius: Process logging and background-worker teardown ordering only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> exposes the
#     retained close-time futures through wait_for_shutdown_completion().
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> joins any
#     scheduler thread that survived its bounded close().
# ============================================================================
def _defer_logging_restore_until_workers_finish(
    *,
    configuration: LoggingConfiguration,
    scheduler: GroupSyncScheduler | None,
    executor: ConnectorJobExecutor | None,
) -> None:
    """Release output now and redaction only after all survivors exit."""

    # FIX: Output ownership and redaction safety are different lifetimes.
    # Restore levels/detach the UMS output handler now, but keep the exact
    # safety filters on every configured handler until both wait contracts
    # positively confirm that no background thread can emit another record.
    release_logging_output(configuration)

    def _wait_and_restore() -> None:
        try:
            if scheduler is not None:
                scheduler.wait_for_shutdown_completion()
            if executor is not None:
                executor.wait_for_shutdown_completion()
        except Exception:  # noqa: BLE001 — safety must remain fail-closed
            # FIX: An observer exception is not a termination edge. Releasing
            # the safety filters here let a still-running audit/worker thread
            # publish raw exception SQL or credentials through foreign/root
            # handlers. Retaining one safety lease is the safe failure mode.
            logger.exception("Background-worker completion wait failed")
            return
        restore_logging(configuration)

    watcher = threading.Thread(
        target=_wait_and_restore,
        name="ums-logging-release-watcher",
        daemon=True,
    )
    try:
        watcher.start()
    except Exception:
        # A watcher-start failure falls back to the same positive termination
        # checks synchronously. If a check fails, _wait_and_restore deliberately
        # retains redaction safety rather than guessing that workers stopped.
        logger.exception("Logging-release watcher failed to start; waiting synchronously")
        _wait_and_restore()


def create_app(*, database_url: str | None = None, authz_source: str | None = None) -> FastAPI:
    """Create the FastAPI application with optional SQL-backed authorization."""
    settings = load_app_settings()
    resolved_database_url = database_url or settings.database_url
    resolved_authz_source = (authz_source or settings.authz_source).strip().lower()
    if resolved_authz_source not in {AUTHZ_SOURCE_HEADERS, AUTHZ_SOURCE_DATABASE}:
        raise ValueError("authz_source must be 'headers' or 'database'")
    if resolved_authz_source == AUTHZ_SOURCE_DATABASE and not resolved_database_url:
        raise ValueError("database authz_source requires database_url or UMS_DATABASE_URL")

    # ========================================================================
    # Purpose: Install the process logging configuration on startup, then on
    #   shutdown close the module-owned GroupSyncScheduler and
    #   ConnectorJobExecutor so the tick thread and worker threads tear down
    #   deterministically (each one's weakref.finalize GC backstop is a safety
    #   net, not the primary teardown), and finally release the logging state.
    # Database/ORM: None directly; both workers own their own session factories.
    # Standards: getattr-guarded so a disabled app (no scheduler, no executor)
    #   shuts down cleanly. Fail-closed default OFF means the import-time app
    #   spawns no threads. Close ORDER matters: scheduler FIRST (stop ticking,
    #   so it can submit no further jobs), THEN executor (drain in-flight
    #   workers) -- closing the executor first would let a scheduler tick
    #   submit into an already-shutting-down pool. When bounded close leaves a
    #   survivor, a daemon completion watcher releases output but retains the
    #   redaction-safety lease until both workers actually finish; otherwise
    #   restore_logging releases both leases inline.
    #   Logging is configured HERE and not in create_app because create_app
    #   runs at import (`app = create_app()` at the foot of this module) --
    #   configuring there would make importing this module reconfigure the
    #   importing process's logging, which is how a test suite becomes
    #   order-dependent. The lifespan runs once per served process.
    # Blast Radius: Process lifecycle / threads / root logger only. No finance,
    #   auth, audit, or graph projection impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/config/logging_config.py ->
    #     configure_logging / restore_logging.
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> close().
    #   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> close().
    # ========================================================================
    @asynccontextmanager
    async def _lifespan(fastapi_app: FastAPI):
        """Configure logging, serve, then close the workers and release logging."""
        logging_configuration = configure_logging(level=settings.log_level)
        try:
            executor = getattr(fastapi_app.state, "connector_job_executor", None)
            if executor is not None:
                # FIX: Recover the prior process's committed-but-undispatched
                # job intents before this process can accept requests or start
                # the scheduler. audit_logs remains the durable handoff when a
                # shutdown audit thread is killed mid-write.
                executor.recover_abandoned_submission_intents()
            scheduler = getattr(fastapi_app.state, "group_sync_scheduler", None)
            if scheduler is not None:
                scheduler.start()
            yield
        finally:
            shutdown_errors: list[Exception] = []
            scheduler_clean = True
            executor_clean = True
            scheduler = getattr(fastapi_app.state, "group_sync_scheduler", None)
            executor = getattr(fastapi_app.state, "connector_job_executor", None)
            try:
                # Scheduler first (stop ticking), then executor (drain workers)
                # -- see the Standards note above for why the order matters.
                if scheduler is not None:
                    try:
                        scheduler_clean = scheduler.close()
                    except Exception as exc:  # noqa: BLE001 — preserve after full cleanup
                        logger.exception("Group-sync scheduler close failed")
                        scheduler_clean = False
                        shutdown_errors.append(exc)
                if executor is not None:
                    try:
                        executor_clean = executor.close()
                        if not executor_clean:
                            logger.error(
                                "Connector executor shutdown was not clean; durable "
                                "submission intents will be reconciled at next startup"
                            )
                    except Exception as exc:  # noqa: BLE001 — preserve after logging release
                        logger.exception("Connector executor close failed")
                        executor_clean = False
                        shutdown_errors.append(exc)
            finally:
                # FIX: A bounded-close survivor retains this lifespan's lease
                # only until the explicit completion APIs observe every worker
                # and scheduler thread exit. Exactly one branch owns restore.
                if scheduler_clean and executor_clean:
                    restore_logging(logging_configuration)
                else:
                    _defer_logging_restore_until_workers_finish(
                        configuration=logging_configuration,
                        scheduler=scheduler,
                        executor=executor,
                    )
            if shutdown_errors:
                raise ExceptionGroup("background worker shutdown failed", shutdown_errors)

    _app = FastAPI(
        title="UMS Smart Revenue Control Center API",
        version="0.1.0",
        description="Numbers-first internal revenue control API for UMS.",
        lifespan=_lifespan,
    )

    if resolved_database_url:
        session_factory = build_session_factory(resolved_database_url)
        sqlite_database = _is_sqlite_database_url(resolved_database_url)
        platform_session_factory = build_platform_session_factory(resolved_database_url)
        _wire_connector_background_workers(
            _app,
            settings=settings,
            session_factory=session_factory,
        )
        _configure_database_dependencies(
            _app,
            resolved_authz_source=resolved_authz_source,
            sqlite_database=sqlite_database,
            session_factory=session_factory,
            platform_session_factory=platform_session_factory,
        )

    _app.include_router(adsense_router)
    _app.include_router(allocation_router)
    _app.include_router(audit_router)
    _app.include_router(channel_account_links_router)
    _app.include_router(channels_router)
    _app.include_router(connectors_router)
    _app.include_router(exchange_rates_router)
    _app.include_router(export_templates_router)
    _app.include_router(exports_router)
    _app.include_router(finance_close_router)
    _app.include_router(groups_router)
    _app.include_router(org_units_router)
    _app.include_router(reconciliation_router)
    _app.include_router(reports_router)
    _app.include_router(revenue_router)
    _app.include_router(security_router)
    _app.include_router(session_router)
    _app.include_router(source_rows_router)
    _app.include_router(tenants_router)
    _app.include_router(users_router)

    def health_payload() -> dict[str, object]:
        """Return service and pinned runtime health metadata."""
        return {
            "status": "ok",
            "service": "ums-smart-revenue",
            "runtime": {
                "python": STACK_VERSION_BASELINE["runtime"]["python"],
                "fastapi": STACK_VERSION_BASELINE["backend"]["fastapi"],
                "pydantic": STACK_VERSION_BASELINE["backend"]["pydantic"],
            },
        }

    @_app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        """Return service health status."""
        return health_payload()

    @_app.get("/livez", tags=["system"])
    def livez() -> dict[str, object]:
        """Return liveness probe status."""
        return health_payload()

    return _app


# ============================================================================
# Purpose: Attach the connector background workers (job executor + group-sync
#   scheduler) to the app state, enforcing the fail-fast settings contract
#   BEFORE any thread starts. Extracted from create_app so the factory stays
#   under the cyclomatic-complexity budget; guards and construction order are
#   unchanged. Called only when a database URL resolved, so both flags are
#   inert without one -- matching the previous inline behavior.
# Database/ORM: None directly; both workers take the request-scoped
#   session_factory and own their per-job/tick session lifecycle.
# Standards: Fail-fast ValueError on misconfiguration; threads start only
#   after every guard passes.
# Blast Radius: Process lifecycle / threads only. No finance, auth, audit, or
#   export impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> ConnectorJobExecutor.
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> GroupSyncScheduler.
#   - File: backend/ums_smart_revenue/config/settings.py -> AppSettings flags.
# ============================================================================
def _wire_connector_background_workers(
    fastapi_app: FastAPI,
    *,
    settings: AppSettings,
    session_factory: SessionFactory,
) -> None:
    """Create the enabled connector workers and store them on ``app.state``."""
    if settings.connector_job_executor_enabled:
        # FIX: reuse the request-scoped session_factory the app already
        # created. build_session_factory caches the engine per URL, but
        # calling it twice still produced a second sessionmaker object
        # over the same engine -- a wasted allocation that the request
        # path and the worker pool both had to share. ThreadPoolExecutor
        # workers call session_factory() per job, so they pick up the
        # same pooled connection lifecycle the request handlers do.
        fastapi_app.state.connector_job_executor = ConnectorJobExecutor(
            session_factory=session_factory,
            max_workers=settings.connector_job_max_workers,
            stale_running_hours=settings.connector_job_stale_running_hours,
        )
    if settings.group_sync_schedule_enabled:
        if not settings.connector_job_executor_enabled:
            raise ValueError(
                "UMS_GROUP_SYNC_SCHEDULE_ENABLED requires"
                " UMS_CONNECTOR_JOB_EXECUTOR_ENABLED to be enabled"
                " -- a scheduler with nothing to submit to is a"
                " misconfiguration"
            )
        if settings.google_connector_service_actor_id is None:
            raise ValueError(
                "UMS_GROUP_SYNC_SCHEDULE_ENABLED requires"
                " UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID to be set"
                " -- the scheduler has no identity to submit jobs as"
            )
        scheduler = GroupSyncScheduler(
            session_factory=session_factory,
            executor=fastapi_app.state.connector_job_executor,
            interval_seconds=settings.group_sync_interval_hours * 3600,
            service_actor_id=settings.google_connector_service_actor_id,
        )
        fastapi_app.state.group_sync_scheduler = scheduler


# ============================================================================
# Purpose: Install the database-backed dependency overrides (sessions,
#   registries, audit sinks, principal loader) and the authz-mode middleware.
#   Extracted from create_app so the factory stays under the cyclomatic-
#   complexity budget; override and middleware order is verbatim.
# Database/ORM: Wires SQLAlchemy session factories into FastAPI dependencies;
#   no table or query changes.
# Standards: Atomic-lane audit sinks for all-or-nothing routes; platform-lane
#   session for tenant resolution under database authz.
# Blast Radius: Authorization (principal source + tenant resolver middleware)
#   and audit-sink routing -- moved verbatim, no behavior change.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> override targets.
#   - File: backend/ums_smart_revenue/api/dependencies_audit.py -> the
#     audit-sink factories swapped in here.
#   - File: backend/ums_smart_revenue/api/registry_dependencies.py -> the
#     channel/group registry factories swapped in here.
# ============================================================================
def _configure_database_dependencies(
    fastapi_app: FastAPI,
    *,
    resolved_authz_source: str,
    sqlite_database: bool,
    session_factory: SessionFactory,
    platform_session_factory: SessionFactory,
) -> None:
    """Install session/registry/sink overrides and authz middleware on ``app``."""
    overrides = fastapi_app.dependency_overrides
    if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
        overrides[current_db_session] = authenticated_session_dependency(session_factory)
        overrides[current_platform_db_session] = (
            _sqlite_platform_session_from_request
            if sqlite_database
            else authenticated_session_dependency(platform_session_factory)
        )
    else:
        overrides[current_db_session] = session_dependency(session_factory)
        overrides[current_platform_db_session] = (
            _sqlite_platform_session_from_request
            if sqlite_database
            else session_dependency(platform_session_factory)
        )
        fastapi_app.add_middleware(DefaultTenantMiddleware)
    overrides[current_channel_registry] = sql_channel_registry_from_session
    overrides[current_group_registry] = sql_group_registry_from_session
    overrides[current_audit_sink] = sql_audit_sink_from_session
    # All-or-nothing routes (the bulk channel import, the CMS group sync)
    # must commit their audit rows atomically with their domain writes, so
    # their sink runs on the request's tenant session (platform-lane
    # elevated per append) instead of the independent platform session.
    overrides[current_atomic_audit_sink] = sql_atomic_audit_sink_from_session
    overrides[current_revenue_audit_sink] = sql_revenue_audit_sink_from_session
    if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
        overrides[current_principal_from_headers] = current_principal_from_database
        # ============================================================
        # Purpose: The tenant resolver reads `tenants` to map slug->tenant
        #   BEFORE TENANT_CTX is set, so its session has no tenant and the
        #   app_tenant lane never activates. Run that lookup on the platform
        #   lane (app_platform) which the session hook switches to via
        #   session.info regardless of context, so it holds the grants a
        #   restricted (INHERIT FALSE) login otherwise lacks.
        # Blast Radius: Authorization/tenant resolution; reads platform
        #   `tenants` only (no RLS table), so BYPASSRLS is immaterial here.
        # ============================================================
        fastapi_app.add_middleware(
            TrustedGatewayTenantResolverMiddleware,
            session_factory=platform_session_factory,
            authorize_tenant=_allow_database_auth_tenant,
        )


def _allow_database_auth_tenant(_scope: object, _tenant_slug: str) -> bool:
    """Allow active tenant resolution before SQL principal loading checks identity."""
    return True


def _is_sqlite_database_url(database_url: str) -> bool:
    """Return whether the configured database URL targets SQLite."""
    return make_url(database_url).get_backend_name() == "sqlite"


# ============================================================================
# Purpose: SQLite test apps cannot hold separate operational and platform write
#   sessions in the same request without risking file-level lock contention.
#   Reuse the already-open request session for platform-only dependencies while
#   Postgres production paths keep the dedicated app_platform session factory.
# Database/ORM: SQLAlchemy request Session; no table shape changes.
# Standards: FastAPI dependency caching preserves one commit/rollback owner.
# Blast Radius: Test SQLite audit writes only; no authorization, finance, Neo4j,
#   or export behavior changes on Postgres.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> dependency target.
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py -> audit sink session.
# ============================================================================
def _sqlite_platform_session_from_request(
    session: Session = Depends(current_db_session),
) -> Session:
    """Return the current request session for SQLite platform dependencies."""
    return session


class TrustedGatewayTenantResolverMiddleware:
    """Resolve tenant context only after trusted gateway headers are valid."""

    def __init__(
        self,
        asgi_app: ASGIApp,
        session_factory: TenantSessionFactory,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
        authorize_tenant: TenantAuthorizer | None = None,
    ) -> None:
        """Initialise with the inner ASGI app, session factory, and optional bypass paths."""
        self._bypass_paths = _normalise_bypass_paths(bypass_paths)
        self._resolver = TenantResolverMiddleware(
            asgi_app,
            session_factory=session_factory,
            bypass_paths=self._bypass_paths,
            authorize_tenant=authorize_tenant,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Validate the trusted-gateway token before delegating to the tenant resolver."""
        if scope["type"] != "http" or scope.get("method", "").upper() == "OPTIONS":
            await self._resolver(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if _tenant_resolution_bypassed(path, self._bypass_paths):
            await self._resolver(scope, receive, send)
            return

        gateway_error = _trusted_gateway_error(scope)
        if gateway_error is not None:
            await _send_http_exception(gateway_error, scope, receive, send)
            return

        await self._resolver(scope, receive, send)


class DefaultTenantMiddleware:
    """Bind the bootstrap UMS tenant for SQL-backed trusted-header requests."""

    def __init__(
        self,
        asgi_app: ASGIApp,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
    ) -> None:
        """Initialise with the inner ASGI app and optional bypass paths."""
        self.app = asgi_app
        self._bypass_paths = _normalise_bypass_paths(bypass_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the bootstrap tenant context and delegate to the inner app."""
        if scope["type"] != "http" or scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if _tenant_resolution_bypassed(path, self._bypass_paths):
            await self.app(scope, receive, send)
            return

        token = TENANT_CTX.set(_bootstrap_tenant())
        try:
            await self.app(scope, receive, send)
        finally:
            TENANT_CTX.reset(token)


def _trusted_gateway_error(scope: Scope) -> HTTPException | None:
    """Validate trusted gateway headers; return an HTTPException if they are invalid."""
    headers = Headers(scope=scope)
    try:
        current_trusted_gateway_identity(
            x_user_id=headers.get("x-user-id"),
            x_ums_trusted_gateway_token=headers.get("x-ums-trusted-gateway-token"),
        )
    except HTTPException as exc:
        return exc
    return None


async def _send_http_exception(
    exc: HTTPException,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    """Write an HTTP exception response directly to the ASGI send channel."""
    await JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )(scope, receive, send)


def _bootstrap_tenant() -> Tenant:
    """Return the bootstrap UMS tenant for single-tenant trusted-header requests."""
    now = datetime.now(UTC)
    return Tenant(
        id=UUID(UMS_TENANT_ID),
        slug="ums",
        display_name="UMS",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _tenant_resolution_bypassed(path: str, bypass_paths: Iterable[str]) -> bool:
    """Return True if the request path matches a configured bypass path."""
    return any(path == bypass or path.startswith(bypass + "/") for bypass in bypass_paths)


app = create_app()
