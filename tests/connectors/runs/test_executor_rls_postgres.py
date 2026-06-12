"""Postgres RLS proof for the ConnectorJobExecutor Bucket-A audit write.

SQLite cannot exercise ``platform_lane`` (a tenant-lane write to ``audit_logs``
is a no-op on SQLite but ``InsufficientPrivilege`` on RLS-enforced Postgres --
the PR #94 lesson). This pins the executor's ``_audit_failed_before_start`` path:
on a real tenant-lane session, the ``platform_lane`` elevation is what lets the
worker persist the platform-only-write ``CONNECTOR_JOB_RUN`` audit row. Without
the elevation the INSERT would deny and the row would be absent.

``require_postgres_url()`` raises (never skips) when ``UMS_TEST_DATABASE_URL`` is
unset, preserving the no-skip policy. This test EXECUTES only in the clean-room
PG gate (a disposable ``postgres:18-alpine`` container), not the per-task SQLite
run.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.db._pg_schema_helpers import reset_public_schema
from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.connectors.google.errors import OAuthRefreshError
from ums_smart_revenue.connectors.runs.executor import (
    ConnectorJobActor,
    ConnectorJobExecutor,
)
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.db.session import build_session_factory

CONNECTOR_KEY = "youtube_reporting"
ACCOUNT_ID = "acct-1"
REPORT_MONTH = "2026-03"
RESOLVER_REF = "secret-manager://ums/yt/acct-1"

_UPGRADED: set[str] = set()


def _alembic_config(url: str) -> Config:
    """Alembic config bound to ``url`` without an ini path (no logging poison)."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option(
        "script_location", "backend/ums_smart_revenue/db/alembic"
    )
    return cfg


def _ensure_fresh_head(url: str) -> None:
    """Reset the public schema and migrate to head once per test session."""
    if url in _UPGRADED:
        return
    reset_public_schema(url)
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED.add(url)


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL and migrate it to head."""
    url = require_postgres_url()
    _ensure_fresh_head(url)
    return url


@pytest.fixture
def fresh_tenant() -> UUID:
    """A unique ACTIVE tenant id per test so the shared schema stays clean."""
    return uuid4()


def _seed_owner(url: str, *, tenant_id: UUID) -> UUID:
    """Seed an ACTIVE tenant + user + active credential as the schema owner.

    RLS is ENABLE (not FORCE), so the owner login bypasses the policies and can
    write every table directly. Returns the seeded user id (the submitting
    actor) for the executor's audit actor snapshot.
    """
    engine = sa.create_engine(url)
    actor_id = uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, slug, display_name) VALUES "
                    "(:id, :slug, :name) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": tenant_id, "slug": f"t-{tenant_id}", "name": "T"},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tid, :email, :name)"
                ),
                {
                    "id": actor_id,
                    "tid": tenant_id,
                    "email": f"seed-{actor_id}@example.com",
                    "name": "Seed",
                },
            )
            conn.execute(
                sa.text(
                    "INSERT INTO api_connector_credentials (id, tenant_id, "
                    "connector_key, account_id, encrypted_secret_ref, status, "
                    "created_by, updated_by) VALUES (:id, :tid, :key, :acct, "
                    ":ref, 'active', :by, :by)"
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "key": CONNECTOR_KEY,
                    "acct": ACCOUNT_ID,
                    "ref": RESOLVER_REF,
                    "by": actor_id,
                },
            )
    finally:
        engine.dispose()
    return actor_id


def test_bucket_a_audit_persists_under_platform_lane_on_postgres(
    pg_url: str, fresh_tenant: UUID
) -> None:
    """The Bucket-A audit row commits on the tenant-lane worker via platform_lane.

    Patch ``run_one`` to raise an ``OAuthRefreshError`` (Bucket A). The executor
    catches it and writes one ``CONNECTOR_JOB_RUN`` job_failed_before_start audit
    row. On RLS-enforced Postgres that INSERT (audit_logs is platform-only-write)
    only succeeds because ``_audit_failed_before_start`` elevates via
    ``platform_lane``; without it the row would be absent.
    """
    actor_id = _seed_owner(pg_url, tenant_id=fresh_tenant)
    actor = ConnectorJobActor(
        user_id=str(actor_id),
        email="ops@example.com",
        role="revenue_operations_admin",
    )
    factory = build_session_factory(pg_url)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )

    def _boom(session, **kwargs):
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            # Synchronous (no thread) so the assertion is deterministic.
            executor._run_job(
                tenant_id=fresh_tenant,
                connector_key=CONNECTOR_KEY,
                account_id=ACCOUNT_ID,
                report_month=REPORT_MONTH,
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=actor,
            )
    finally:
        executor.close()

    # Read back via the owner engine so the assertions are not themselves
    # subject to the RLS lane under test.
    engine = sa.create_engine(pg_url)
    try:
        with Session(engine) as read:
            rows = list(
                read.scalars(
                    select(AuditLogORM)
                    .where(AuditLogORM.tenant_id == fresh_tenant)
                    .where(AuditLogORM.event_type == "CONNECTOR_JOB_RUN")
                )
            )
    finally:
        engine.dispose()

    assert len(rows) == 1
    row = rows[0]
    assert row.details["action"] == "job_failed_before_start"
    assert row.details["error_class"] == "OAuthRefreshError"
    # Canned class name only -- the exception text must never be persisted.
    assert "revoked" not in str(row.details)
