"""Behaviour tests for :class:`SqlAlchemyTenantRepository`."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.models import TenantStatus
from ums_smart_revenue.tenancy.repository import (
    SqlAlchemyTenantRepository,
    TenantNotFoundError,
    TenantValidationError,
)


def _make_engine() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    TenantBase.metadata.create_all(engine)
    return engine


def _seed_tenant(
    session: Session,
    *,
    slug: str = "ums",
    display_name: str = "UMS",
    primary_currency: str = "USD",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> TenantORM:
    now = datetime.now(UTC)
    row = TenantORM(
        id=uuid4(),
        slug=slug,
        display_name=display_name,
        primary_currency=primary_currency,
        status=status.value,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _make_tenant_row(**kwargs: object) -> TenantORM:
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


def test_get_by_slug_returns_active_tenant():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        seeded = _seed_tenant(session)
        repository = SqlAlchemyTenantRepository(session)

        tenant = repository.get_by_slug("ums")

        assert tenant.slug == "ums"
        assert tenant.id == seeded.id
        assert tenant.status == TenantStatus.ACTIVE
        assert tenant.is_active is True


def test_get_by_slug_normalises_whitespace_and_casing():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        _seed_tenant(session, slug="rotana")
        repository = SqlAlchemyTenantRepository(session)

        assert repository.get_by_slug("  RoTaNa  ").slug == "rotana"


def test_get_by_slug_rejects_blank():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        repository = SqlAlchemyTenantRepository(session)

        for bad in ("", "   ", "\t\n"):
            with pytest.raises(TenantValidationError):
                repository.get_by_slug(bad)


def test_get_by_slug_raises_not_found_for_unknown():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        repository = SqlAlchemyTenantRepository(session)

        with pytest.raises(TenantNotFoundError):
            repository.get_by_slug("nope")


def test_get_by_id_returns_tenant():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        seeded = _seed_tenant(session, slug="news")
        repository = SqlAlchemyTenantRepository(session)

        tenant = repository.get_by_id(UUID(str(seeded.id)))

        assert tenant.slug == "news"
        assert tenant.id == seeded.id


def test_get_by_id_raises_not_found_for_unknown_uuid():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        repository = SqlAlchemyTenantRepository(session)

        with pytest.raises(TenantNotFoundError):
            repository.get_by_id(uuid4())


def test_suspended_status_is_propagated_to_domain():
    engine = _make_engine()
    with Session(engine) as session, session.begin():
        _seed_tenant(session, slug="paused", status=TenantStatus.SUSPENDED)
        repository = SqlAlchemyTenantRepository(session)

        tenant = repository.get_by_slug("paused")

        assert tenant.status == TenantStatus.SUSPENDED
        assert tenant.is_active is False


def test_to_domain_rejects_naive_datetimes():
    from ums_smart_revenue.tenancy.repository import _to_domain

    row = _make_tenant_row(created_at=datetime.now(UTC).replace(tzinfo=None))

    with pytest.raises(ValueError, match="invalid timezone for created_at"):
        _to_domain(row)


def test_to_domain_rejects_invalid_currency():
    from ums_smart_revenue.tenancy.repository import _to_domain

    row = _make_tenant_row(primary_currency="usd")

    with pytest.raises(
        ValueError,
        match="invalid primary_currency: must be 3 uppercase letters",
    ):
        _to_domain(row)


def test_to_domain_rejects_invalid_status():
    from ums_smart_revenue.tenancy.repository import _to_domain

    row = _make_tenant_row(status="DELETED")

    with pytest.raises(ValueError, match="invalid tenant status: 'DELETED'"):
        _to_domain(row)
