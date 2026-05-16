"""FastAPI application factory and router wiring."""

from fastapi import FastAPI

from ums_smart_revenue.api.adsense import router as adsense_router
from ums_smart_revenue.api.audit import router as audit_router
from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
    sql_audit_sink_from_session,
    sql_channel_registry_from_session,
)
from ums_smart_revenue.api.channels import (
    router as channels_router,
)
from ums_smart_revenue.api.connectors import router as connectors_router
from ums_smart_revenue.api.dependencies import (
    authenticated_session_dependency,
    current_db_session,
    current_principal_from_database,
    current_principal_from_headers,
)
from ums_smart_revenue.api.exchange_rates import router as exchange_rates_router
from ums_smart_revenue.api.exports import router as exports_router
from ums_smart_revenue.api.finance_close import router as finance_close_router
from ums_smart_revenue.api.groups import (
    router as groups_router,
)
from ums_smart_revenue.api.registry_dependencies import (
    current_group_registry,
    sql_group_registry_from_session,
)
from ums_smart_revenue.api.reports import router as reports_router
from ums_smart_revenue.api.revenue import (
    current_revenue_audit_sink,
    sql_revenue_audit_sink_from_session,
)
from ums_smart_revenue.api.revenue import router as revenue_router
from ums_smart_revenue.api.security import router as security_router
from ums_smart_revenue.api.users import router as users_router
from ums_smart_revenue.config.settings import (
    AUTHZ_SOURCE_DATABASE,
    AUTHZ_SOURCE_HEADERS,
    load_app_settings,
)
from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE
from ums_smart_revenue.db.session import build_session_factory, session_dependency


def create_app(
    *, database_url: str | None = None, authz_source: str | None = None
) -> FastAPI:
    """Create the FastAPI application with optional SQL-backed authorization."""
    settings = load_app_settings()
    resolved_database_url = database_url or settings.database_url
    resolved_authz_source = (authz_source or settings.authz_source).strip().lower()
    if resolved_authz_source not in {AUTHZ_SOURCE_HEADERS, AUTHZ_SOURCE_DATABASE}:
        raise ValueError("authz_source must be 'headers' or 'database'")
    if resolved_authz_source == AUTHZ_SOURCE_DATABASE and not resolved_database_url:
        raise ValueError(
            "database authz_source requires database_url or UMS_DATABASE_URL"
        )
    app = FastAPI(
        title="UMS Smart Revenue Control Center API",
        version="0.1.0",
        description="Numbers-first internal revenue control API for UMS.",
    )

    if resolved_database_url:
        session_factory = build_session_factory(resolved_database_url)
        overrides = app.dependency_overrides
        if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
            overrides[current_db_session] = authenticated_session_dependency(
                session_factory
            )
        else:
            overrides[current_db_session] = session_dependency(session_factory)
        overrides[current_channel_registry] = sql_channel_registry_from_session
        overrides[current_group_registry] = sql_group_registry_from_session
        overrides[current_audit_sink] = sql_audit_sink_from_session
        overrides[current_revenue_audit_sink] = sql_revenue_audit_sink_from_session
        if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
            overrides[current_principal_from_headers] = current_principal_from_database

    app.include_router(adsense_router)
    app.include_router(audit_router)
    app.include_router(channels_router)
    app.include_router(connectors_router)
    app.include_router(exchange_rates_router)
    app.include_router(exports_router)
    app.include_router(finance_close_router)
    app.include_router(groups_router)
    app.include_router(reports_router)
    app.include_router(revenue_router)
    app.include_router(security_router)
    app.include_router(users_router)

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

    @app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        return health_payload()

    @app.get("/livez", tags=["system"])
    def livez() -> dict[str, object]:
        return health_payload()

    return app


app = create_app()
