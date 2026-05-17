"""Integration tests for :class:`TenantResolverMiddleware`."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import StreamingResponse

from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy import (
    DEFAULT_BYPASS_PATHS,
    TENANT_HEADER,
    Tenant,
    TenantResolverMiddleware,
    TenantStatus,
    get_current_tenant,
)


def _build_engine() -> Engine:
    # TestClient runs the ASGI app in a worker thread, so the middleware's
    # session_factory needs the SAME underlying SQLite in-memory connection
    # the test thread used for metadata.create_all. StaticPool + the
    # check_same_thread escape hatch give us that.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    TenantBase.metadata.create_all(engine)
    return engine


def _seed(engine: Engine, **kwargs: object) -> TenantORM:
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    row = TenantORM(
        id=kwargs.get("id", uuid4()),
        slug=kwargs.get("slug", "ums"),
        display_name=kwargs.get("display_name", "UMS"),
        primary_currency=kwargs.get("primary_currency", "USD"),
        status=kwargs.get("status", TenantStatus.ACTIVE).value
        if isinstance(kwargs.get("status", TenantStatus.ACTIVE), TenantStatus)
        else kwargs.get("status", "ACTIVE"),
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )
    with factory() as session, session.begin():
        session.add(row)
    return row


def _build_app(engine: Engine) -> FastAPI:
    """Bare FastAPI app with the resolver middleware and a probe endpoint."""

    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        tenant = get_current_tenant()
        if tenant is None:
            return {"tenant": None}
        return {"tenant": {"slug": tenant.slug, "status": tenant.status.value}}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def _gen() -> Iterator[str]:
            tenant = get_current_tenant()
            yield tenant.slug if tenant is not None else "none"

        return StreamingResponse(_gen(), media_type="text/plain")

    return app


def test_active_tenant_request_succeeds_and_sets_context():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 200
    assert response.json() == {"tenant": {"slug": "ums", "status": "ACTIVE"}}


def test_missing_tenant_header_returns_400():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami")

    assert response.status_code == 400
    assert response.json()["header"] == TENANT_HEADER


def test_blank_tenant_header_returns_400():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "   "})

    assert response.status_code == 400


def test_unknown_tenant_returns_404():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "rotana"})

    assert response.status_code == 404
    assert "rotana" in response.json()["detail"]


def test_suspended_tenant_returns_423():
    engine = _build_engine()
    _seed(engine, slug="paused", status=TenantStatus.SUSPENDED)
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "paused"})

    assert response.status_code == 423
    assert "paused" in response.json()["detail"]


def test_archived_tenant_returns_410():
    engine = _build_engine()
    _seed(engine, slug="retired", status=TenantStatus.ARCHIVED)
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "retired"})

    assert response.status_code == 410


def test_health_endpoint_bypasses_resolver():
    engine = _build_engine()
    client = TestClient(_build_app(engine))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tenant_context_is_cleared_after_response():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})
    assert response.status_code == 200
    # After the request finishes, ambient context goes back to None.
    assert get_current_tenant() is None


def test_tenant_authorizer_denies_before_context_is_set():
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: False,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None


def test_missing_tenant_authorizer_denies_by_default():
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(TenantResolverMiddleware, session_factory=factory)

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None


def test_streaming_response_keeps_tenant_context_until_body_consumed():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    with client.stream("GET", "/stream", headers={TENANT_HEADER: "ums"}) as response:
        assert response.status_code == 200
        assert list(response.iter_text()) == ["ums"]

    assert get_current_tenant() is None


def test_resolver_runs_blocking_lookup_off_event_loop_thread():
    now = datetime.now(UTC)
    tenant = Tenant(
        id=uuid4(),
        slug="ums",
        display_name="UMS",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )
    observed: dict[str, int] = {}

    class RecordingTenantResolverMiddleware(TenantResolverMiddleware):
        def _resolve(self, raw_slug: str) -> Tenant:
            observed["resolve_thread"] = threading.get_ident()
            return tenant

    app = FastAPI()
    app.add_middleware(
        RecordingTenantResolverMiddleware,
        session_factory=lambda: None,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/thread")
    async def thread_probe() -> dict[str, str]:
        observed["endpoint_thread"] = threading.get_ident()
        return {"tenant": get_current_tenant().slug}

    client = TestClient(app)

    response = client.get("/thread", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 200
    assert response.json() == {"tenant": "ums"}
    assert observed["resolve_thread"] != observed["endpoint_thread"]


def test_default_bypass_paths_match_documented_set():
    expected = {"/health", "/livez", "/readyz", "/docs", "/redoc", "/openapi.json"}
    assert set(DEFAULT_BYPASS_PATHS) == expected


def test_slug_is_normalised_to_lowercase_on_resolution():
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "  UMS  "})

    assert response.status_code == 200
    assert response.json() == {"tenant": {"slug": "ums", "status": "ACTIVE"}}
