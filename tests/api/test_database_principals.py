"""Regression tests for SQL-backed principal authorization."""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    TrustedGatewayIdentity,
    current_principal_from_database,
    current_trusted_gateway_identity,
)
from ums_smart_revenue.app import TrustedGatewayTenantResolverMiddleware, create_app
from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.principals import (
    MAX_ACTIVE_PERMISSION_GRANTS,
    MAX_ACTIVE_ROLE_ASSIGNMENTS,
    PRINCIPAL_QUERY_TIMEOUT_MS,
    PrincipalLoadError,
    PrincipalNotFoundError,
    SqlAlchemyPrincipalLoader,
)
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    AuditLogORM,
    PermissionORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

ACTOR_ID = UUID("00000000-0000-0000-0000-000000016001")
TARGET_ID = UUID("00000000-0000-0000-0000-000000016002")
GLOBAL_SCOPE_ID = UUID("00000000-0000-0000-0000-000000016101")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000016201")
ROLE_ASSIGNMENT_OVERFLOW_COUNT = MAX_ACTIVE_ROLE_ASSIGNMENTS + 1
PERMISSION_GRANT_OVERFLOW_COUNT = MAX_ACTIVE_PERMISSION_GRANTS + 1


class RecordingConnection:
    """Connection double that records transaction-local SQL commands."""

    def __init__(self) -> None:
        """Initialize the recorded command list."""
        self.executed_sql: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        """Record raw SQL executed against the connection."""
        self.executed_sql.append(sql)


class FailingSession:
    """Session double that raises during the user row lookup."""

    def __init__(
        self,
        *,
        active_transaction: bool = False,
        dialect_name: str = "sqlite",
        lookup_error: Exception | None = None,
    ) -> None:
        """Initialize the failing session and its observable counters."""
        self.active_transaction = active_transaction
        self.connection_count = 0
        self.dialect_name = dialect_name
        self.get_count = 0
        self.lookup_error = lookup_error or SQLAlchemyTimeoutError(
            "database unavailable"
        )
        self.recording_connection = RecordingConnection()
        self.rollback_count = 0

    def in_transaction(self) -> bool:
        """Report no active transaction so the loader prepares isolation."""
        return self.active_transaction

    def get_bind(self) -> SimpleNamespace:
        """Return a minimal bind with the dialect name used by the loader."""
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect_name))

    @staticmethod
    def begin() -> nullcontext:
        """Return a no-op context manager for the loader transaction scope."""
        return nullcontext()

    def connection(self, *, execution_options: dict[str, str]) -> RecordingConnection:
        """Accept read isolation options before the failing lookup."""
        assert execution_options == {"isolation_level": "SERIALIZABLE"}
        self.connection_count += 1
        return self.recording_connection

    def get(self, *_args: object, **_kwargs: object) -> None:
        """Raise the same base error SQLAlchemy uses for storage failures."""
        self.get_count += 1
        raise self.lookup_error

    def rollback(self) -> None:
        """Record rollback calls between retry attempts."""
        self.rollback_count += 1


class UnusedSession:
    """Context manager that records unexpected session allocation."""

    def __enter__(self):
        """Enter the fake session context."""
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        """Exit the fake session context without swallowing exceptions."""
        return False

    def commit(self) -> None:
        """Accept commits if the dependency unexpectedly reaches cleanup."""

    def rollback(self) -> None:
        """Accept rollbacks if the dependency unexpectedly reaches cleanup."""


class SessionAllocationCounter:
    """Session factory double that counts allocation attempts."""

    def __init__(self) -> None:
        """Initialize the session allocation counter."""
        self.opened_sessions = 0

    def build_session_factory(self, _database_url: str):
        """Return a factory that records every attempted session allocation."""

        def factory() -> UnusedSession:
            """Record that a session would have been allocated."""
            self.opened_sessions += 1
            return UnusedSession()

        return factory


def auth_headers(
    user_id: UUID = ACTOR_ID,
    *,
    claimed_role: str = "assistant_analyst",
    include_bootstrap_claims: bool = True,
) -> dict[str, str]:
    """Build trusted gateway headers for database-mode request tests."""
    headers = {
        "x-user-id": str(user_id),
        "x-ums-tenant": "ums",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if include_bootstrap_claims:
        headers.update(
            {
                "x-user-email": "ignored-header@example.com",
                "x-role": claimed_role,
                "x-scope-type": "global",
            }
        )
    return headers


def build_database_url(tmp_path) -> str:
    """Return an isolated SQLite URL for a pytest temp directory."""
    return f"sqlite+pysqlite:///{(tmp_path / 'database-principals.db').as_posix()}"


@contextmanager
def ums_tenant_context():
    """Bind the request tenant context expected by database principal loading."""
    now = datetime.now(UTC)
    token = TENANT_CTX.set(
        Tenant(
            id=UUID(UMS_TENANT_ID),
            slug="ums",
            display_name="UMS",
            primary_currency="USD",
            status=TenantStatus.ACTIVE,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    try:
        yield
    finally:
        TENANT_CTX.reset(token)


def seed_security_catalog(session: Session) -> AccessScopeORM:
    """Seed the role, permission, and global-scope rows required by auth tests."""
    scope = AccessScopeORM(id=GLOBAL_SCOPE_ID, scope_type="global", label="Global")
    session.add(scope)
    for definition in ROLE_DEFINITIONS.values():
        session.add(
            RoleORM(
                key=definition.role.value,
                label=definition.label,
                description=definition.description,
                service_only=definition.service_only,
            )
        )
    for definition in PERMISSION_DEFINITIONS.values():
        session.add(
            PermissionORM(
                key=definition.permission.value,
                label=definition.label,
                sensitive=definition.sensitive,
                audit_on_use=definition.audit_on_use,
            )
        )
    return scope


def seed_database(database_url: str) -> None:
    """Create the security schema and seed the two users used by auth tests."""
    engine = create_engine(database_url)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            TenantORM(
                id=UUID(UMS_TENANT_ID),
                slug="ums",
                display_name="UMS",
                primary_currency="USD",
                status="ACTIVE",
            )
        )
        seed_security_catalog(session)
        session.add_all(
            [
                UserORM(
                    id=ACTOR_ID,
                    email="actor@example.com",
                    display_name="Actor User",
                ),
                UserORM(
                    id=TARGET_ID,
                    email="target@example.com",
                    display_name="Target User",
                ),
            ]
        )
        session.commit()


def add_role_assignment(database_url: str, *, user_id: UUID, role_key: str) -> None:
    """Insert one active role assignment for a test user."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            UserRoleAssignmentORM(
                id=uuid4(),
                user_id=user_id,
                role_key=role_key,
                scope_id=GLOBAL_SCOPE_ID,
                assigned_by=user_id,
                reason="Seed DB-backed principal role",
                active=True,
            )
        )
        session.commit()


def add_direct_permission(
    database_url: str,
    *,
    user_id: UUID,
    permission_key: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> None:
    """Insert one active direct permission grant for a test user."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        scope_row_id = GLOBAL_SCOPE_ID
        if scope_type != "global":
            scope_row_id = uuid4()
            session.add(
                AccessScopeORM(
                    id=scope_row_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    label=f"{scope_type}:{scope_id}",
                )
            )
        session.add(
            UserPermissionGrantORM(
                id=uuid4(),
                user_id=user_id,
                permission_key=permission_key,
                scope_id=scope_row_id,
                granted_by=user_id,
                reason="Seed DB-backed principal permission",
                active=True,
            )
        )
        session.commit()


def add_many_role_assignments(database_url: str, *, count: int) -> None:
    """Insert enough active role rows to exercise principal-load limits."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        # SQLite cannot express the Postgres-only partial global-scope index.
        session.execute(text("DROP INDEX IF EXISTS uq_access_scopes_global_singleton"))
        for index in range(count):
            scope = AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id=f"overflow-company-{index}",
                label=f"Overflow company {index}",
            )
            session.add(scope)
            session.add(
                UserRoleAssignmentORM(
                    id=uuid4(),
                    user_id=ACTOR_ID,
                    role_key="company_manager",
                    scope_id=scope.id,
                    assigned_by=ACTOR_ID,
                    reason="Seed over-limit DB-backed principal role",
                    active=True,
                )
            )
        session.commit()


def add_many_permission_grants(database_url: str, *, count: int) -> None:
    """Insert enough direct permission rows to exercise principal-load limits."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        # SQLite cannot express the Postgres-only partial global-scope index.
        session.execute(text("DROP INDEX IF EXISTS uq_access_scopes_global_singleton"))
        for index in range(count):
            scope = AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id=f"overflow-grant-company-{index}",
                label=f"Overflow grant company {index}",
            )
            session.add(scope)
            session.add(
                UserPermissionGrantORM(
                    id=uuid4(),
                    user_id=ACTOR_ID,
                    permission_key="analytics.view",
                    scope_id=scope.id,
                    granted_by=ACTOR_ID,
                    reason="Seed over-limit DB-backed principal permission",
                    active=True,
                )
            )
        session.commit()


def test_database_principal_uses_stored_role_instead_of_claimed_header_role(tmp_path):
    """Database mode authorizes from stored roles without bootstrap claims."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_role_assignment(database_url, user_id=ACTOR_ID, role_key="corporate_admin")
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers(include_bootstrap_claims=False),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "global",
            "reason": "DB role should authorize this assignment",
        },
    )

    assert response.status_code == 201
    assert response.json()["role_key"] == "assistant_analyst"
    assert response.json()["assigned_by"] == str(ACTOR_ID)


def test_database_principal_loads_direct_permission_grants(tmp_path):
    """Direct SQL permission grants authorize guarded endpoints."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_direct_permission(database_url, user_id=ACTOR_ID, permission_key="audit.view")
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            AuditLogORM(
                id=uuid4(),
                user_id=ACTOR_ID,
                event_type="CHANNEL_UPDATED",
                entity_type="youtube_channel",
                entity_id="channel-1",
                scope_type="global",
                details={"field": "company_id"},
                sensitive=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get(
        "/audit/events?limit=10",
        headers=auth_headers(claimed_role="assistant_analyst"),
    )

    assert response.status_code == 200
    assert [item["event_type"] for item in response.json()["items"]] == [
        "CHANNEL_UPDATED"
    ]


def test_database_principal_uses_stored_user_tenant(tmp_path):
    """Principal tenant identity comes from the persisted user row."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))

    assert principal.tenant_id == UMS_TENANT_ID


def test_database_principal_rejects_mismatched_explicit_tenant(tmp_path):
    """Explicit tenant input cannot make a user appear in another tenant."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session, pytest.raises(
        PrincipalNotFoundError, match="User is not registered"
    ):
        SqlAlchemyPrincipalLoader(session).load(
            user_id=str(ACTOR_ID),
            tenant_id=str(OTHER_TENANT_ID),
        )


def test_database_principal_rejects_disabled_user_with_super_owner_header(tmp_path):
    """Disabled SQL users fail closed even if headers claim super-owner."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        actor = session.get(UserORM, ACTOR_ID)
        assert actor is not None
        actor.status = "disabled"
        session.commit()
    add_role_assignment(database_url, user_id=ACTOR_ID, role_key="super_owner")
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get(
        "/security/roles",
        headers=auth_headers(claimed_role="super_owner"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_database_principal_rejects_unregistered_user(tmp_path):
    """Unknown SQL users fail closed with a generic forbidden response."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get(
        "/security/roles",
        headers=auth_headers(user_id=uuid4(), claimed_role="super_owner"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_database_principal_sanitizes_invalid_user_id(tmp_path):
    """Malformed user ids return a generic invalid-request response."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    headers = auth_headers(claimed_role="super_owner")
    headers["x-user-id"] = "not-a-uuid"

    response = client.get("/security/roles", headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid request"


def test_database_principal_rejects_malformed_user_id_before_opening_session(
    monkeypatch,
):
    """Malformed nonblank user ids are rejected before DB session allocation."""
    counter = SessionAllocationCounter()
    monkeypatch.setattr(
        "ums_smart_revenue.app.build_session_factory", counter.build_session_factory
    )
    client = TestClient(
        create_app(
            database_url="sqlite+pysqlite:///:memory:",
            authz_source="database",
        )
    )
    headers = auth_headers(include_bootstrap_claims=False)
    headers["x-user-id"] = "not-a-uuid"

    response = client.get("/security/roles", headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid request"
    assert counter.opened_sessions == 0


def test_database_principal_canonicalizes_user_id_before_session():
    """Valid user ids are normalized before session-backed loading."""
    mixed_case_user_id = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"

    identity = current_trusted_gateway_identity(
        x_user_id=mixed_case_user_id,
        x_ums_trusted_gateway_token=auth_headers()["x-ums-trusted-gateway-token"],
    )

    assert identity.user_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_database_principal_rejects_blank_user_id_as_missing_header(tmp_path):
    """Whitespace-only user ids are treated as missing auth headers."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    headers = auth_headers(claimed_role="super_owner")
    headers["x-user-id"] = "   "

    response = client.get("/security/roles", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing authentication headers"


def test_database_principal_rejects_bad_token_before_opening_session(monkeypatch):
    """Invalid gateway tokens are rejected before allocating a DB session."""
    counter = SessionAllocationCounter()
    monkeypatch.setattr(
        "ums_smart_revenue.app.build_session_factory", counter.build_session_factory
    )
    client = TestClient(
        create_app(
            database_url="sqlite+pysqlite:///:memory:",
            authz_source="database",
        )
    )
    headers = auth_headers(include_bootstrap_claims=False)
    headers["x-ums-trusted-gateway-token"] = "invalid-token"

    response = client.get("/security/roles", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted gateway token"
    assert counter.opened_sessions == 0


"""
Test module for database tenant resolver behavior, including the health endpoint.
This module ensures that bypass paths are normalized before authentication and that the health
endpoint correctly reports the service status.
"""

def test_database_tenant_resolver_normalizes_bypass_paths_before_auth():
    """Database-auth wrapper bypasses normalized paths before gateway checks."""
    app = FastAPI()
    app.add_middleware(
        TrustedGatewayTenantResolverMiddleware,
        session_factory=lambda: None,
        bypass_paths=("/health/",),
        authorize_tenant=lambda _scope, _slug: True,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Health endpoint returning the overall service status."""
        return {"status": "ok"}

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_database_principal_retryable_storage_errors_return_service_unavailable():
    """Retryable SQLAlchemy storage failures are retried once before 503."""
    session = FailingSession()

    with ums_tenant_context(), pytest.raises(HTTPException) as exc_info:
        current_principal_from_database(
            identity=TrustedGatewayIdentity(user_id=str(ACTOR_ID)),
            session=session,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Principal authorization unavailable"
    assert session.get_count == 2
    assert session.rollback_count == 1


def test_database_principal_requires_tenant_context():
    """Principal loading fails closed before storage reads when tenant context is absent."""
    session = FailingSession()

    with pytest.raises(HTTPException) as exc_info:
        current_principal_from_database(
            identity=TrustedGatewayIdentity(user_id=str(ACTOR_ID)),
            session=session,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Principal authorization unavailable"
    assert session.get_count == 0


def test_database_principal_non_retryable_storage_errors_do_not_retry():
    """Non-transient SQLAlchemy failures fail closed without retry storms."""
    session = FailingSession(lookup_error=SQLAlchemyError("statement is invalid"))

    with ums_tenant_context(), pytest.raises(HTTPException) as exc_info:
        current_principal_from_database(
            identity=TrustedGatewayIdentity(user_id=str(ACTOR_ID)),
            session=session,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Principal authorization unavailable"
    assert session.get_count == 1
    assert session.rollback_count == 0


def test_database_principal_unexpected_errors_return_service_unavailable():
    """Unexpected loader failures are fail-closed instead of leaking as 500s."""
    session = FailingSession(lookup_error=RuntimeError("driver bridge failed"))

    with ums_tenant_context(), pytest.raises(HTTPException) as exc_info:
        current_principal_from_database(
            identity=TrustedGatewayIdentity(user_id=str(ACTOR_ID)),
            session=session,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Principal authorization unavailable"
    assert session.get_count == 1
    assert session.rollback_count == 0


def test_database_principal_uses_serializable_postgres_reads():
    """Postgres principal reads request serializable isolation and a timeout."""
    session = FailingSession(dialect_name="postgresql")

    with pytest.raises(SQLAlchemyError):
        SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))

    assert PRINCIPAL_QUERY_TIMEOUT_MS == 5_000
    assert session.connection_count == 1
    assert session.recording_connection.executed_sql == [
        f"SET LOCAL statement_timeout = {PRINCIPAL_QUERY_TIMEOUT_MS}"
    ]


def test_database_principal_rejects_active_transaction_to_avoid_race_reads():
    """Caller-owned transactions are rejected to avoid mixed-isolation races."""
    session = FailingSession(active_transaction=True)

    with ums_tenant_context(), pytest.raises(HTTPException) as exc_info:
        current_principal_from_database(
            identity=TrustedGatewayIdentity(user_id=str(ACTOR_ID)),
            session=session,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Principal authorization unavailable"
    assert session.connection_count == 0
    assert session.get_count == 0


def test_database_principal_corrupt_stored_role_returns_service_unavailable(
    tmp_path,
):
    """Corrupt stored role keys are treated as service-unavailable data errors."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_role_assignment(database_url, user_id=ACTOR_ID, role_key="not_a_role")
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get(
        "/security/roles",
        headers=auth_headers(claimed_role="super_owner"),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Principal authorization unavailable"


def test_database_principal_enforces_direct_permission_scope_isolation(tmp_path):
    """A company-scoped direct grant does not satisfy a global audit read."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_direct_permission(
        database_url,
        user_id=ACTOR_ID,
        permission_key="audit.view",
        scope_type="company",
        scope_id=str(TARGET_ID),
    )
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get(
        "/audit/events?limit=10",
        headers=auth_headers(claimed_role="assistant_analyst"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: audit.view"


def test_database_principal_accepts_role_assignment_limit_boundary(tmp_path):
    """Exactly 256 active role assignments are accepted for one principal."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_many_role_assignments(database_url, count=MAX_ACTIVE_ROLE_ASSIGNMENTS)
    engine = create_engine(database_url)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))

    assert len(principal.role_assignments) == MAX_ACTIVE_ROLE_ASSIGNMENTS


def test_database_principal_rejects_too_many_active_role_assignments(tmp_path):
    """Role assignment overflow fails before building the principal."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_many_role_assignments(database_url, count=ROLE_ASSIGNMENT_OVERFLOW_COUNT)
    engine = create_engine(database_url)

    with (
        Session(engine) as session,
        pytest.raises(PrincipalLoadError, match="role assignments"),
    ):
        SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))


def test_database_principal_accepts_permission_grant_limit_boundary(tmp_path):
    """Exactly 512 active direct permission grants are accepted."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_many_permission_grants(database_url, count=MAX_ACTIVE_PERMISSION_GRANTS)
    engine = create_engine(database_url)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))

    assert len(principal.direct_permissions) == MAX_ACTIVE_PERMISSION_GRANTS


def test_database_principal_rejects_too_many_active_permission_grants(tmp_path):
    """Direct permission grant overflow fails before building the principal."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_many_permission_grants(database_url, count=PERMISSION_GRANT_OVERFLOW_COUNT)
    engine = create_engine(database_url)

    with (
        Session(engine) as session,
        pytest.raises(PrincipalLoadError, match="permission grants"),
    ):
        SqlAlchemyPrincipalLoader(session).load(user_id=str(ACTOR_ID))


def test_audit_endpoint_fails_closed_without_database_session():
    """Audit reads fail closed when no database session dependency is wired."""
    client = TestClient(create_app())

    response = client.get(
        "/audit/events?limit=10",
        headers=auth_headers(claimed_role="super_owner"),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "database session not configured"
