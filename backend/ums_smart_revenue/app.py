from fastapi import FastAPI

from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
    router as channels_router,
    sql_audit_sink_from_session,
    sql_channel_registry_from_session,
)
from ums_smart_revenue.api.connectors import router as connectors_router
from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.api.finance_close import router as finance_close_router
from ums_smart_revenue.api.groups import (
    current_group_registry,
    router as groups_router,
    sql_group_registry_from_session,
)
from ums_smart_revenue.api.revenue import router as revenue_router
from ums_smart_revenue.api.security import router as security_router
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE
from ums_smart_revenue.db.session import build_session_factory, session_dependency


def create_app(*, database_url: str | None = None) -> FastAPI:
    resolved_database_url = database_url or load_app_settings().database_url
    app = FastAPI(
        title="UMS Smart Revenue Control Center API",
        version="0.1.0",
        description="Numbers-first internal revenue control API for UMS.",
    )

    if resolved_database_url:
        session_factory = build_session_factory(resolved_database_url)
        app.dependency_overrides[current_db_session] = session_dependency(session_factory)
        app.dependency_overrides[current_channel_registry] = sql_channel_registry_from_session
        app.dependency_overrides[current_group_registry] = sql_group_registry_from_session
        app.dependency_overrides[current_audit_sink] = sql_audit_sink_from_session

    app.include_router(channels_router)
    app.include_router(connectors_router)
    app.include_router(finance_close_router)
    app.include_router(groups_router)
    app.include_router(revenue_router)
    app.include_router(security_router)

    @app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "ums-smart-revenue",
            "runtime": {
                "python": STACK_VERSION_BASELINE["runtime"]["python"],
                "fastapi": STACK_VERSION_BASELINE["backend"]["fastapi"],
                "pydantic": STACK_VERSION_BASELINE["backend"]["pydantic"],
            },
        }

    return app


app = create_app()
