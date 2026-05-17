"""Behaviour tests for the ``tenant_id`` field added to :class:`UserPrincipal`.

The field is brand-new in S2.3 and intentionally defaults to ``None`` so
every pre-existing call site keeps working unchanged. S2.4 will tighten
the contract (resolver middleware sets it; routes will assume it).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.principals import SqlAlchemyPrincipalLoader
from ums_smart_revenue.db.security_models import SecurityBase, UserORM


def test_user_principal_defaults_tenant_id_to_none():
    principal = UserPrincipal(user_id="user-1", email="user@example.com")

    assert principal.tenant_id is None


def test_user_principal_round_trips_explicit_tenant_id():
    tenant_id = str(uuid4())

    principal = UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        tenant_id=tenant_id,
    )

    assert principal.tenant_id == tenant_id


def test_user_principal_is_frozen():
    principal = UserPrincipal(user_id="user-1", email="user@example.com")

    with pytest.raises(Exception):  # FrozenInstanceError subclass of Exception
        principal.tenant_id = str(uuid4())  # type: ignore[misc]


def test_principal_loader_passes_tenant_id_through_when_provided():
    engine = _build_engine_with_users()
    user_id = uuid4()
    tenant_id = uuid4()
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(
            user_id=str(user_id), tenant_id=str(tenant_id)
        )

    assert principal.user_id == str(user_id)
    assert principal.tenant_id == str(tenant_id)


def test_principal_loader_omits_tenant_id_when_unspecified():
    engine = _build_engine_with_users()
    user_id = uuid4()
    _seed_active_user(engine, user_id=user_id)

    with Session(engine) as session:
        principal = SqlAlchemyPrincipalLoader(session).load(user_id=str(user_id))

    assert principal.tenant_id is None


def _build_engine_with_users():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    SecurityBase.metadata.create_all(engine)
    return engine


def _seed_active_user(engine, *, user_id: UUID) -> None:
    now = datetime.now(timezone.utc)
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
