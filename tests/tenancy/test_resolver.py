"""Integration tests for :class:`TenantResolverMiddleware`."""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
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
from ums_smart_revenue.tenancy.repository import MAX_TENANT_SLUG_LENGTH
from ums_smart_revenue.tenancy.resolver import _ensure_active_tenant, _ResolverError


def _build_engine() -> Engine:
    """Create a thread-shareable in-memory SQLite tenant registry."""
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
        """Expose PostgreSQL's UUID helper to SQLite-backed tests."""
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    TenantBase.metadata.create_all(engine)
    return engine


def _seed(engine: Engine, **kwargs: object) -> TenantORM:
    """Insert one tenant row for resolver integration tests."""
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


def _make_tenant_row(**kwargs: object) -> TenantORM:
    """Build a detached tenant row for fake-session failure tests."""
    now = datetime.now(UTC)
    return TenantORM(
        id=kwargs.get("id", uuid4()),
        slug=kwargs.get("slug", "ums"),
        display_name=kwargs.get("display_name", "UMS"),
        primary_currency=kwargs.get("primary_currency", "USD"),
        status=kwargs.get("status", TenantStatus.ACTIVE).value
        if isinstance(kwargs.get("status", TenantStatus.ACTIVE), TenantStatus)
        else kwargs.get("status", "ACTIVE"),
        onboarding_at=kwargs.get("onboarding_at", now),
        created_at=kwargs.get("created_at", now),
        updated_at=kwargs.get("updated_at", now),
    )


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
        """Return the tenant visible from request context."""
        tenant = get_current_tenant()
        if tenant is None:
            return {"tenant": None}
        return {"tenant": {"slug": tenant.slug, "status": tenant.status.value}}

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return a simple bypass-path health response."""
        return {"status": "ok"}

    @app.get("/stream")
    def stream() -> StreamingResponse:
        """Stream the tenant slug while response body iteration is active."""

        def _gen() -> Iterator[str]:
            """Yield the tenant slug from inside the streaming iterator."""
            tenant = get_current_tenant()
            yield tenant.slug if tenant is not None else "none"

        return StreamingResponse(_gen(), media_type="text/plain")

    return app


def _build_registry_failure_app(session_factory: Callable[[], object]) -> FastAPI:
    """Build an app whose tenant registry dependency fails during resolution."""
    app = FastAPI()
    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=session_factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return the tenant if middleware unexpectedly lets the request through."""
        tenant = get_current_tenant()
        return {"tenant": tenant.slug if tenant is not None else None}

    return app


def test_active_tenant_request_succeeds_and_sets_context():
    """A valid active tenant header resolves and reaches the endpoint."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 200
    assert response.json() == {"tenant": {"slug": "ums", "status": "ACTIVE"}}
    assert response.headers["vary"] == TENANT_HEADER


def test_missing_tenant_header_returns_400():
    """Missing tenant headers are rejected as bad client input."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami")

    assert response.status_code == 400
    assert response.json()["header"] == TENANT_HEADER
    assert response.headers["vary"] == TENANT_HEADER


def test_blank_tenant_header_returns_400():
    """Blank tenant headers are rejected before registry lookup."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "   "})

    assert response.status_code == 400


def test_invalid_tenant_headers_are_rejected_before_session_factory():
    """Bad tenant headers return 400 without opening registry sessions."""
    app = FastAPI()
    calls: list[str] = []

    def unexpected_session_factory() -> object:
        """Record an unexpected lookup attempt before failing the test."""
        calls.append("opened")
        raise TenantRegistryUnavailableError

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=unexpected_session_factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return context if invalid headers unexpectedly reach the endpoint."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    cases = (
        {},
        {TENANT_HEADER: "   "},
        {TENANT_HEADER: "a" * (MAX_TENANT_SLUG_LENGTH + 1)},
    )
    for headers in cases:
        response = client.get("/whoami", headers=headers)
        assert response.status_code == 400
        assert response.json()["header"] == TENANT_HEADER

    assert calls == []


def test_unknown_tenant_returns_404():
    """Unknown tenant slugs map to a not-found response."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "rotana"})

    assert response.status_code == 404
    assert "rotana" in response.json()["detail"]


def test_suspended_tenant_returns_423():
    """Suspended tenants are identified but blocked from request handling."""
    engine = _build_engine()
    _seed(engine, slug="paused", status=TenantStatus.SUSPENDED)
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "paused"})

    assert response.status_code == 423
    assert "paused" in response.json()["detail"]


def test_archived_tenant_returns_410():
    """Archived tenants are identified but treated as gone."""
    engine = _build_engine()
    _seed(engine, slug="retired", status=TenantStatus.ARCHIVED)
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "retired"})

    assert response.status_code == 410


def test_health_endpoint_bypasses_resolver():
    """Configured health endpoints do not need a tenant header."""
    engine = _build_engine()
    client = TestClient(_build_app(engine))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_options_requests_bypass_tenant_resolution_without_header():
    """OPTIONS probes pass through without tenant headers or registry lookup."""
    app = FastAPI()

    def unexpected_session_factory() -> object:
        """Fail the test if OPTIONS incorrectly reaches tenant lookup."""
        raise OptionsLookupError

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=unexpected_session_factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.options("/whoami")
    def preflight() -> dict[str, str]:
        """Return a synthetic preflight response."""
        return {"status": "preflight"}

    client = TestClient(app)

    response = client.options("/whoami")

    assert response.status_code == 200
    assert response.json() == {"status": "preflight"}
    assert get_current_tenant() is None


def test_mutating_methods_and_streaming_bodies_resolve_tenant_context():
    """Tenant resolution works for mutating methods and streamed bodies."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.api_route("/mutate", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def mutate(request: Request) -> dict[str, object]:
        """Echo method, body, and tenant context for non-GET requests."""
        tenant = get_current_tenant()
        body = await request.body()
        return {
            "method": request.method,
            "body": body.decode("utf-8"),
            "tenant": tenant.slug if tenant is not None else None,
        }

    def body_chunks() -> Iterator[bytes]:
        """Yield a chunked request body for middleware pass-through coverage."""
        yield b"streamed"
        yield b"-body"

    client = TestClient(app)
    cases: list[tuple[str, object, str]] = [
        ("POST", body_chunks(), "streamed-body"),
        ("PUT", b"put-body", "put-body"),
        ("PATCH", b"patch-body", "patch-body"),
        ("DELETE", b"", ""),
    ]

    for method, content, expected_body in cases:
        response = client.request(
            method,
            "/mutate",
            headers={TENANT_HEADER: "ums"},
            content=content,
        )
        assert response.status_code == 200
        assert response.json() == {
            "method": method,
            "body": expected_body,
            "tenant": "ums",
        }
        assert response.headers["vary"] == TENANT_HEADER


def test_websocket_scopes_bypass_tenant_resolution_without_header():
    """Non-HTTP ASGI scopes pass through without tenant lookup."""
    app = FastAPI()

    def unexpected_session_factory() -> object:
        """Fail the test if websocket scope reaches tenant lookup."""
        raise OptionsLookupError

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=unexpected_session_factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.websocket("/ws")
    async def websocket_probe(websocket: WebSocket) -> None:
        """Return whether tenant context leaked into websocket handling."""
        await websocket.accept()
        await websocket.send_json({"tenant": get_current_tenant()})
        await websocket.close()

    client = TestClient(app)

    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json() == {"tenant": None}


def test_tenant_context_is_cleared_after_response():
    """Tenant context resets after the request task finishes."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})
    assert response.status_code == 200
    # After the request finishes, ambient context goes back to None.
    assert get_current_tenant() is None


def test_concurrent_requests_keep_tenant_context_isolated():
    """Overlapping tenant requests see only their own context values."""
    now = datetime.now(UTC)
    tenants = {
        slug: Tenant(
            id=uuid4(),
            slug=slug,
            display_name=slug.title(),
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
        for slug in ("alpha", "beta")
    }

    class StaticTenantResolverMiddleware(TenantResolverMiddleware):
        """Resolver variant that avoids SQLite concurrency in this test."""

        def _resolve(self, normalised_slug: str) -> Tenant:
            """Return the tenant matching the normalized slug."""
            return tenants[normalised_slug]

    app = FastAPI()
    app.add_middleware(
        StaticTenantResolverMiddleware,
        session_factory=lambda: None,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/whoami")
    async def whoami() -> dict[str, object]:
        """Return the tenant visible after another request has overlapped."""
        await asyncio.sleep(0.01)
        tenant = get_current_tenant()
        if tenant is None:
            return {"tenant": None}
        return {"tenant": {"slug": tenant.slug, "status": tenant.status.value}}

    async def run_requests() -> list[dict[str, object]]:
        """Issue two tenant-scoped requests against one ASGI app."""
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            responses = await asyncio.gather(
                client.get("/whoami", headers={TENANT_HEADER: "alpha"}),
                client.get("/whoami", headers={TENANT_HEADER: "beta"}),
            )
        return [response.json() for response in responses]

    alpha, beta = asyncio.run(run_requests())

    assert alpha == {"tenant": {"slug": "alpha", "status": "ACTIVE"}}
    assert beta == {"tenant": {"slug": "beta", "status": "ACTIVE"}}
    assert get_current_tenant() is None


def test_tenant_authorizer_denies_before_context_is_set():
    """Authorization denial still happens before tenant context is exposed."""
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
        """Return raw context so the denial test can prove it is unset."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None


def test_lookup_and_lifecycle_errors_precede_authorization_denial():
    """Lookup and tenant lifecycle responses keep documented status mapping."""
    engine = _build_engine()
    _seed(engine, slug="active")
    _seed(engine, slug="paused", status=TenantStatus.SUSPENDED)
    _seed(engine, slug="retired", status=TenantStatus.ARCHIVED)
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: False,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return context if authorization or lifecycle checks are bypassed."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    responses = {
        "missing": client.get("/whoami", headers={TENANT_HEADER: "missing"}),
        "paused": client.get("/whoami", headers={TENANT_HEADER: "paused"}),
        "retired": client.get("/whoami", headers={TENANT_HEADER: "retired"}),
        "active": client.get("/whoami", headers={TENANT_HEADER: "active"}),
    }

    assert responses["missing"].status_code == 404
    assert responses["paused"].status_code == 423
    assert responses["retired"].status_code == 410
    assert responses["active"].status_code == 403
    assert get_current_tenant() is None


def test_missing_tenant_authorizer_denies_by_default():
    """The resolver fails closed when no authorization callback is supplied."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(TenantResolverMiddleware, session_factory=factory)

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return raw context if default-deny unexpectedly allows access."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None


class AuthorizerUnavailableError(RuntimeError):
    """Stable exception raised by the authorizer failure test."""

    def __init__(self) -> None:
        """Create a deterministic message for log assertions."""
        super().__init__("authorization service unavailable")


def test_tenant_authorizer_errors_fail_closed(caplog: pytest.LogCaptureFixture):
    """Authorization callback exceptions are logged and converted to 403."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    caplog.set_level(logging.WARNING, logger="ums_smart_revenue.tenancy.resolver")

    def broken_authorizer(_scope, _slug) -> bool:
        """Simulate an unavailable authorization dependency."""
        raise AuthorizerUnavailableError

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=broken_authorizer,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return raw context if fail-closed authorization breaks."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None
    assert "Tenant authorization callback failed" in caplog.text
    assert "authorization service unavailable" in caplog.text
    [record] = [
        record
        for record in caplog.records
        if record.message.startswith("Tenant authorization callback failed")
    ]
    assert record.path == "/whoami"
    assert record.scope_type == "http"
    assert record.tenant_slug == "ums"


class TenantRegistryUnavailableError(RuntimeError):
    """Stable exception raised when a registry session cannot be created."""

    def __init__(self) -> None:
        """Create a deterministic message for log assertions."""
        super().__init__("tenant registry session unavailable")


class DatabaseOfflineError(RuntimeError):
    """Stable exception raised by the database failure test."""

    def __init__(self) -> None:
        """Create a deterministic message for log assertions."""
        super().__init__("database offline")


class SessionCloseUnavailableError(RuntimeError):
    """Stable exception raised by session close failure tests."""

    def __init__(self) -> None:
        """Create a deterministic message for log assertions."""
        super().__init__("tenant registry close failed")


class OptionsLookupError(AssertionError):
    """Raised if OPTIONS incorrectly reaches tenant lookup in tests."""

    def __init__(self) -> None:
        """Create a deterministic message for preflight bypass assertions."""
        super().__init__("OPTIONS must bypass tenant lookup")


class ExecutorSubmissionError(RuntimeError):
    """Stable exception raised when blocking-work submission fails in tests."""

    def __init__(self) -> None:
        """Create a deterministic message for executor failure assertions."""
        super().__init__("executor unavailable")


class _ScalarResult:
    """Tiny stand-in for SQLAlchemy's scalar result object."""

    def __init__(self, row: TenantORM) -> None:
        """Store the row returned by ``one_or_none``."""
        self._row = row

    def one_or_none(self) -> TenantORM:
        """Return the single fake tenant row."""
        return self._row


class _CorruptTenantSession:
    """Session fake that returns a malformed tenant registry row."""

    def scalars(self, _statement: object) -> _ScalarResult:
        """Return a row that fails repository domain validation."""
        return _ScalarResult(_make_tenant_row(primary_currency="usd"))

    def close(self) -> None:
        """Satisfy the resolver's session cleanup contract."""


class _BrokenDatabaseSession:
    """Session fake that raises a SQLAlchemy lookup failure."""

    def __init__(self, closed: list[bool]) -> None:
        """Track whether middleware still closes failed sessions."""
        self._closed = closed

    def scalars(self, _statement: object) -> object:
        """Raise a representative database error from the lookup path."""
        raise OperationalError("SELECT tenants", {}, DatabaseOfflineError())

    def close(self) -> None:
        """Record that the resolver cleaned up after the failed lookup."""
        self._closed.append(True)


class _CloseRaisesSession:
    """Session fake whose cleanup fails after lookup validation fails."""

    def scalars(self, _statement: object) -> object:
        """Raise a representative database error from the lookup path."""
        raise OperationalError("SELECT tenants", {}, DatabaseOfflineError())

    def close(self) -> None:
        """Raise a close failure that must not mask the lookup error."""
        raise SessionCloseUnavailableError


def test_session_factory_errors_fail_closed_and_log(
    caplog: pytest.LogCaptureFixture,
):
    """Session factory failures return 503 instead of escaping middleware."""
    caplog.set_level(logging.ERROR, logger="ums_smart_revenue.tenancy.resolver")

    def broken_session_factory() -> object:
        """Fail before a tenant repository session exists."""
        raise TenantRegistryUnavailableError

    client = TestClient(_build_registry_failure_app(broken_session_factory))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant registry unavailable"}
    assert "Tenant registry lookup failed" in caplog.text
    assert "tenant registry session unavailable" in caplog.text


def test_sqlalchemy_lookup_errors_fail_closed_and_close_session(
    caplog: pytest.LogCaptureFixture,
):
    """SQLAlchemy errors from tenant lookup are logged and mapped to 503."""
    caplog.set_level(logging.ERROR, logger="ums_smart_revenue.tenancy.resolver")
    closed: list[bool] = []
    client = TestClient(
        _build_registry_failure_app(lambda: _BrokenDatabaseSession(closed))
    )

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant registry unavailable"}
    assert closed == [True]
    assert "Tenant registry lookup failed" in caplog.text
    assert "database offline" in caplog.text


def test_tenant_lookup_timeout_returns_503(caplog: pytest.LogCaptureFixture):
    """Slow tenant lookups time out and keep blocking admission bounded."""
    now = datetime.now(UTC)
    calls: list[str] = []
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

    class SlowTenantResolverMiddleware(TenantResolverMiddleware):
        """Resolver variant that simulates a stalled registry lookup."""

        def _resolve(self, _normalised_slug: str) -> Tenant:
            """Sleep longer than the configured lookup timeout."""
            calls.append(_normalised_slug)
            time.sleep(0.05)
            return tenant

    app = FastAPI()
    caplog.set_level(logging.ERROR, logger="ums_smart_revenue.tenancy.resolver")
    app.add_middleware(
        SlowTenantResolverMiddleware,
        session_factory=lambda: None,
        authorize_tenant=lambda _scope, _slug: True,
        lookup_timeout_seconds=0.01,
        max_blocking_tasks=1,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return context if the timeout unexpectedly lets the request through."""
        return {"tenant": get_current_tenant()}

    async def run_requests() -> list[object]:
        """Issue overlapping requests against a one-slot blocking-work budget."""
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await asyncio.gather(
                client.get("/whoami", headers={TENANT_HEADER: "ums"}),
                client.get("/whoami", headers={TENANT_HEADER: "ums"}),
            )

    responses = asyncio.run(run_requests())

    assert [response.status_code for response in responses] == [503, 503]
    assert [response.json() for response in responses] == [
        {"detail": "Tenant registry unavailable"},
        {"detail": "Tenant registry unavailable"},
    ]
    assert [response.headers["vary"] for response in responses] == [
        TENANT_HEADER,
        TENANT_HEADER,
    ]
    assert calls == ["ums"]
    assert "Tenant registry lookup timed out" in caplog.text


def test_session_close_errors_do_not_mask_lookup_errors(
    caplog: pytest.LogCaptureFixture,
):
    """Session close failures preserve the original lookup failure response."""
    caplog.set_level(logging.WARNING, logger="ums_smart_revenue.tenancy.resolver")
    client = TestClient(_build_registry_failure_app(_CloseRaisesSession))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant registry unavailable"}
    assert "Tenant registry lookup failed" in caplog.text
    assert "database offline" in caplog.text
    assert "Tenant registry session close failed" in caplog.text
    assert "tenant registry close failed" in caplog.text


def test_lookup_cancellation_is_reraised():
    """Lookup cancellation escapes instead of becoming a synthetic 503."""
    messages: list[dict[str, object]] = []

    async def blocked_app(_scope: object, _receive: object, _send: object) -> None:
        """Fail the test if cancellation lets the request reach the app."""
        raise AssertionError

    class CancellingTenantResolverMiddleware(TenantResolverMiddleware):
        """Resolver variant that simulates request cancellation during lookup."""

        def _resolve(self, _normalised_slug: str) -> Tenant:
            """Raise the cancellation signal the middleware must preserve."""
            raise asyncio.CancelledError

    middleware = CancellingTenantResolverMiddleware(
        blocked_app,
        session_factory=lambda: None,
        authorize_tenant=lambda _scope, _slug: True,
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/whoami",
        "headers": [(TENANT_HEADER.lower().encode("ascii"), b"ums")],
    }

    async def receive() -> dict[str, object]:
        """Return a minimal ASGI request event."""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        """Record any response messages emitted by the middleware."""
        messages.append(message)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(scope, receive, send))

    assert messages == []


def test_authorizer_timeout_fails_closed_and_logs(caplog: pytest.LogCaptureFixture):
    """Slow authorization callbacks are denied and logged."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    caplog.set_level(logging.WARNING, logger="ums_smart_revenue.tenancy.resolver")

    def slow_authorizer(_scope, _slug) -> bool:
        """Sleep longer than the configured authorization timeout."""
        time.sleep(0.05)
        return True

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=slow_authorizer,
        authorization_timeout_seconds=0.01,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return context if timeout handling unexpectedly allows access."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert response.headers["vary"] == TENANT_HEADER
    assert "Tenant authorization callback timed out" in caplog.text


def test_authorizer_executor_failures_fail_closed_and_log(
    caplog: pytest.LogCaptureFixture,
):
    """Authorization executor failures deny access instead of escaping."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    caplog.set_level(logging.WARNING, logger="ums_smart_revenue.tenancy.resolver")

    class BrokenAuthorizationExecutorMiddleware(TenantResolverMiddleware):
        """Resolver variant that fails only authorization offload submission."""

        def _submit_blocking(
            self,
            loop: asyncio.AbstractEventLoop,
            func: Callable[..., object],
            *args: object,
        ) -> asyncio.Future:
            """Raise once the authorization callback is submitted."""
            if getattr(func, "__name__", "") == "_is_authorized":
                raise ExecutorSubmissionError
            return super()._submit_blocking(loop, func, *args)

    app.add_middleware(
        BrokenAuthorizationExecutorMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return context if authorization executor failure allows access."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert response.headers["vary"] == TENANT_HEADER
    assert "Tenant authorization execution failed" in caplog.text
    assert "executor unavailable" in caplog.text


def test_blocking_submission_failures_release_admission_slot():
    """Executor submission failures do not leak blocking-work slots."""
    calls: list[str] = []

    class BrokenSubmissionMiddleware(TenantResolverMiddleware):
        """Resolver variant that always fails executor submission."""

        def _submit_blocking(
            self,
            loop: asyncio.AbstractEventLoop,
            func: Callable[..., object],
            *args: object,
        ) -> asyncio.Future:
            """Raise before a future can receive the done callback."""
            calls.append(func.__name__)
            raise ExecutorSubmissionError

    middleware = BrokenSubmissionMiddleware(
        FastAPI(),
        session_factory=lambda: None,
        authorize_tenant=lambda _scope, _slug: True,
        max_blocking_tasks=1,
    )

    async def run_twice() -> None:
        """Run two failing submissions through the same one-slot limiter."""
        for _ in range(2):
            with pytest.raises(ExecutorSubmissionError):
                await middleware._run_blocking_with_timeout(
                    lambda: None,
                    timeout=0.01,
                )

    asyncio.run(run_twice())

    assert calls == ["<lambda>", "<lambda>"]


def test_corrupt_tenant_registry_rows_fail_closed_and_log(
    caplog: pytest.LogCaptureFixture,
):
    """Malformed tenant rows from the repository are logged and mapped to 503."""
    caplog.set_level(logging.ERROR, logger="ums_smart_revenue.tenancy.resolver")
    client = TestClient(_build_registry_failure_app(_CorruptTenantSession))

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant registry unavailable"}
    assert "Tenant registry lookup failed" in caplog.text
    assert "invalid primary_currency" in caplog.text


def test_oversized_tenant_header_returns_400():
    """Oversized tenant slugs are rejected before registry lookup."""
    engine = _build_engine()
    client = TestClient(_build_app(engine))

    response = client.get(
        "/whoami",
        headers={TENANT_HEADER: "a" * (MAX_TENANT_SLUG_LENGTH + 1)},
    )

    assert response.status_code == 400
    assert "at most" in response.json()["detail"]


def test_authorizer_non_bool_decisions_fail_closed(caplog: pytest.LogCaptureFixture):
    """Authorization hooks must return bool decisions to allow tenant access."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    app = FastAPI()
    factory = sessionmaker(engine, expire_on_commit=False)
    caplog.set_level(logging.WARNING, logger="ums_smart_revenue.tenancy.resolver")
    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=factory,
        authorize_tenant=lambda _scope, _slug: "allow",
    )

    @app.get("/whoami")
    def whoami() -> dict[str, object]:
        """Return raw context if non-bool authorization is accepted."""
        return {"tenant": get_current_tenant()}

    client = TestClient(app)

    response = client.get("/whoami", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access denied"}
    assert get_current_tenant() is None
    assert "non-bool decision" in caplog.text
    [record] = [
        record
        for record in caplog.records
        if record.message == "Tenant authorization callback returned non-bool decision"
    ]
    assert record.decision_type == "str"
    assert record.tenant_slug == "ums"


def test_bypass_paths_reject_empty_root_and_relative_entries():
    """Unsafe bypass path values are rejected during middleware construction."""
    app = FastAPI()

    invalid_bypass_sets = [
        "health",
        ("",),
        ("/",),
        ("health",),
        ("/health", ""),
    ]

    for bypass_paths in invalid_bypass_sets:
        with pytest.raises(ValueError, match=r"bypass path|bypass_paths"):
            TenantResolverMiddleware(
                app,
                session_factory=lambda: None,
                bypass_paths=bypass_paths,
                authorize_tenant=lambda _scope, _slug: True,
            )


def test_unrecognised_tenant_status_fails_closed():
    """Future tenant statuses are not treated as active by default."""
    now = datetime.now(UTC)
    tenant = Tenant(
        id=uuid4(),
        slug="future",
        display_name="Future",
        primary_currency="USD",
        status=cast(TenantStatus, "PAUSED"),
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(_ResolverError) as exc_info:
        _ensure_active_tenant(tenant)

    assert exc_info.value.status_code == 500
    assert "invalid status" in exc_info.value.detail


def test_bypass_paths_are_normalised_before_matching():
    """Bypass paths are deduplicated and stripped before path matching."""
    middleware = TenantResolverMiddleware(
        FastAPI(),
        session_factory=lambda: None,
        bypass_paths=("/health/", "/docs//", "/health"),
        authorize_tenant=lambda _scope, _slug: True,
    )

    assert middleware._should_bypass("/health")
    assert middleware._should_bypass("/health/live")
    assert middleware._should_bypass("/docs")
    assert middleware._bypass_paths == ("/health", "/docs")


def test_streaming_response_keeps_tenant_context_until_body_consumed():
    """Streaming responses can still read context during body iteration."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    with client.stream("GET", "/stream", headers={TENANT_HEADER: "ums"}) as response:
        assert response.status_code == 200
        assert list(response.iter_text()) == ["ums"]

    assert get_current_tenant() is None


def test_resolver_runs_blocking_lookup_off_event_loop_thread():
    """Synchronous tenant lookup runs outside the async endpoint thread."""
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
        """Resolver variant that records where blocking lookup executes."""

        def _resolve(self, _normalised_slug: str) -> Tenant:
            """Capture the thread used by middleware resolution."""
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
        """Capture the endpoint thread and return the resolved tenant slug."""
        observed["endpoint_thread"] = threading.get_ident()
        return {"tenant": get_current_tenant().slug}

    client = TestClient(app)

    response = client.get("/thread", headers={TENANT_HEADER: "ums"})

    assert response.status_code == 200
    assert response.json() == {"tenant": "ums"}
    assert observed["resolve_thread"] != observed["endpoint_thread"]


def test_default_bypass_paths_match_documented_set():
    """The exported default bypass set matches the documented contract."""
    expected = {"/health", "/livez", "/readyz", "/docs", "/redoc", "/openapi.json"}
    assert set(DEFAULT_BYPASS_PATHS) == expected


def test_slug_is_normalised_to_lowercase_on_resolution():
    """Header whitespace and casing are normalized before tenant lookup."""
    engine = _build_engine()
    _seed(engine, slug="ums")
    client = TestClient(_build_app(engine))

    response = client.get("/whoami", headers={TENANT_HEADER: "  UMS  "})

    assert response.status_code == 200
    assert response.json() == {"tenant": {"slug": "ums", "status": "ACTIVE"}}
