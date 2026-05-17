"""Behaviour tests for the ``tenant_id`` field added to :class:`UserPrincipal`.

The field is brand-new in S2.3 and intentionally defaults to ``None`` so
every pre-existing call site keeps working unchanged. S2.4 will tighten
the contract (resolver middleware sets it; routes will assume it).
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.principals import (
    PrincipalValidationError,
    SqlAlchemyPrincipalLoader,
)
from ums_smart_revenue.db.security_models import SecurityBase, UserORM


def test_user_principal_defaults_tenant_id_to_none() -> None:
    """Existing callers get no tenant context until S2.4 wires the resolver."""
    principal = UserPrincipal(user_id="user-1", email="user@example.com")

    assert principal.tenant_id is None


def test_user_principal_round_trips_explicit_tenant_id() -> None:
    """Direct construction preserves the caller-provided canonical tenant id."""
    tenant_id = str(uuid4())

    principal = UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        tenant_id=tenant_id,
    )

    assert principal.tenant_id == tenant_id


def test_user_principal_is_frozen() -> None:
    """Tenant context is immutable once the request principal is built."""
    principal = UserPrincipal(user_id="user-1", email="user@example.com")

    with pytest.raises(FrozenInstanceError):
        principal.tenant_id = str(uuid4())  # type: ignore[misc]


def test_principal_loader_normalizes_tenant_id_when_provided() -> None:
    """Loader output always stores tenant ids in canonical UUID string form."""
    engine = _build_engine_with_users()
    user_id = uuid4()
    tenant_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(
            user_id=str(user_id), tenant_id=str(tenant_id).upper()
        )

    assert principal.user_id == str(user_id)
    assert principal.tenant_id == str(tenant_id)


def test_principal_loader_omits_tenant_id_when_unspecified() -> None:
    """Legacy load paths remain tenantless when no tenant context is supplied."""
    engine = _build_engine_with_users()
    user_id = uuid4()
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(user_id=str(user_id))

    assert principal.tenant_id is None


@pytest.mark.parametrize("tenant_id", ["", "   "])
def test_principal_loader_treats_empty_tenant_id_as_unspecified(
    tenant_id: str,
) -> None:
    """Blank tenant values cannot become authorization context."""
    engine = _build_engine_with_users()
    user_id = uuid4()
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(
            user_id=str(user_id), tenant_id=tenant_id
        )

    assert principal.tenant_id is None


def test_principal_loader_rejects_malformed_tenant_id() -> None:
    """Malformed tenant ids fail before a principal can be constructed."""
    engine = _build_engine_with_users()
    user_id = uuid4()
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session, pytest.raises(PrincipalValidationError):
        SqlAlchemyPrincipalLoader(session).load(
            user_id=str(user_id), tenant_id="not-a-uuid"
        )


def _build_engine_with_users() -> Engine:
    """Create an in-memory security schema for principal-loader tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        """Install a SQLite replacement for the Postgres UUID default."""
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    SecurityBase.metadata.create_all(engine)
    return engine


def _seed_active_user(engine: Engine, *, user_id: UUID) -> None:
    """Insert an active user row that can be loaded as a principal."""
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            UserORM(
                id=user_id,
                email=f"u-{user_id}@example.com",
                display_name="Test",
                status="active",
                is_service_account=False,
                created_at=now,
                updated_at=now,
            )
        )
