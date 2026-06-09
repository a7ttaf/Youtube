from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.session import (
    build_platform_session_factory,
    build_session_factory,
)
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus


@pytest.fixture(autouse=True)
def _schema_at_head():
    """Bring the shared test DB to head so the RLS context helpers exist.

    Sibling PostgreSQL migration tests downgrade/drop the public schema, so
    these hook tests cannot assume a head schema. Upgrading here makes the
    suite order-independent without skipping (UMS_TEST_DATABASE_URL absent =>
    require_postgres_url raises, so SQLite-only runs are unaffected).
    """
    import os

    if not os.environ.get("UMS_TEST_DATABASE_URL"):
        yield
        return
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", require_postgres_url())
    command.upgrade(cfg, "head")
    yield


def _tenant(uuid_str: str) -> Tenant:
    """Build a deterministic tenant object for session-hook tests."""
    from datetime import UTC, datetime
    from uuid import UUID
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Tenant(
        id=UUID(uuid_str), slug="ums", display_name="UMS",
        primary_currency="USD", status=TenantStatus.ACTIVE,
        onboarding_at=now, created_at=now, updated_at=now,
    )


def test_sqlite_session_issues_no_set_statements():
    """Verify the SQLite session hook stays a no-op for tenant context."""
    # On SQLite the hook must be a complete no-op (no SET ROLE / tenant context).
    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    token = TENANT_CTX.set(_tenant("00000000-0000-0000-0000-000000000001"))
    try:
        with factory() as session:
            # A trivial query must not raise (no Postgres-only SQL emitted).
            assert session.execute(sa.text("SELECT 1")).scalar() == 1
    finally:
        TENANT_CTX.reset(token)


def test_postgres_tenant_lane_sets_role_and_trusted_tenant_context():
    """Verify the tenant lane sets app_tenant and the trusted tenant context."""
    url = require_postgres_url()
    factory = build_session_factory(url)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as session:
            # Typed boundary: the getter is RETURNS uuid (context column is
            # uuid), so the driver yields a UUID — compare against UUID(tid).
            assert session.execute(
                sa.text("SELECT app_current_tenant_id()")
            ).scalar() == UUID(tid)
            assert session.execute(
                sa.text("SELECT current_user")
            ).scalar() == "app_tenant"
    finally:
        TENANT_CTX.reset(token)


def test_postgres_no_context_leaves_login_role_and_unset_context():
    """Verify the tenant lane leaves bare sessions on the login role."""
    url = require_postgres_url()
    factory = build_session_factory(url)
    # No TENANT_CTX → hook must not switch role or set the trusted context row.
    with factory() as session:
        assert session.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar() is None
        assert session.execute(
            sa.text("SELECT current_user")
        ).scalar() != "app_tenant"


def test_platform_lane_uses_app_platform_and_no_tenant_context():
    """Verify the platform lane uses app_platform without tenant context."""
    url = require_postgres_url()
    factory = build_platform_session_factory(url)
    with factory() as session:
        assert session.execute(
            sa.text("SELECT current_user")
        ).scalar() == "app_platform"
        assert session.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar() is None


def test_pooled_connection_does_not_leak_role_or_context():
    """Verify a reused pooled connection does not retain tenant session state."""
    # Transaction 1 sets tenant lane; transaction 2 on the SAME pooled
    # connection (no context) must see no leaked role/tenant context.
    url = require_postgres_url()
    engine = sa.create_engine(url, pool_size=1, max_overflow=0)
    factory = build_session_factory(url, engine=engine)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as s1:
            assert s1.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"
            s1.commit()
    finally:
        TENANT_CTX.reset(token)
    # Reuse the pool with no context.
    with factory() as s2:
        assert s2.execute(sa.text("SELECT current_user")).scalar() != "app_tenant"
        assert s2.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar() is None
    engine.dispose()
