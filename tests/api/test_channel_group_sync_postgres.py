"""Postgres-tier proof for POST /channels/groups/sync: RLS isolation + rollback.

The SQLite tier (``tests/api/test_channel_group_sync_api.py``) substitutes the
group store with the in-memory ``ChannelGroupRegistry``, so it cannot settle two
questions that only exist on the real database:

1. **Tenant isolation.** ``channel_groups`` carries row-level security; groups
   mirrored from one tenant's CMS content owner must be invisible to another
   tenant's lane. The bare ``SELECT`` here has no ``WHERE tenant_id`` on
   purpose, mirroring ``tests/api/test_channels_import_postgres.py`` and
   ``tests/tenancy/test_isolation.py`` — RLS is the only filter under test.
2. **All-or-nothing including audit.** A sync applies every changed group and
   emits one ``GROUP_UPDATED`` per change plus a ``GROUPS_SYNCED`` summary. A
   failure part-way through the apply must leave NO group rows and NO audit
   rows behind: a durable ``GROUP_UPDATED`` for a group that does not exist
   would make the trail claim a mirror that never happened.

``require_postgres_url()`` raises (never skips) when ``UMS_TEST_DATABASE_URL``
is unset, preserving the repository's no-skip policy.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.api.channels import current_groups_client_factory
from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
)
from ums_smart_revenue.db.session import build_session_factory
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

CHANNEL_ID = "UCq-Fj5jknLsUf-MWSy4_brA"
SECOND_ID = "UCsT0YIqwnpJCM-mx7-gSA4Q"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
GROUP_ID = "g1"
SECOND_GROUP_ID = "g2"
GROUP_TITLE = "TV Sector"
SYNC_URL = "/channels/groups/sync"

# Tenant A is the bootstrap UMS tenant every trusted-header request binds to.
# Tenant B is the isolation counterpart, seeded exactly as in tests/tenancy.
TENANT_A = UMS_TENANT_ID
TENANT_B = "00000000-0000-0000-0000-000000000002"

_UPGRADED_URLS: set[str] = set()


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without an ini file.

    Mirrors the no-ini pattern in ``tests/tenancy/test_force_rls.py``: env.py
    only touches the logging tree when ``config_file_name`` is set, so
    configuring ``script_location`` + ``sqlalchemy.url`` directly avoids
    silencing other tests' ``caplog`` for the rest of the session.
    """
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


def _ensure_upgraded(url: str) -> None:
    """Migrate the disposable Postgres database to head once per session."""
    if url in _UPGRADED_URLS:
        return
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED_URLS.add(url)


def _tenant(tenant_id: str, slug: str) -> Tenant:
    """Build an ACTIVE tenant object for lane-scoped reads and requests."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Tenant(
        id=UUID(tenant_id),
        slug=slug,
        display_name=slug.upper(),
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _seed_isolation_tenant(engine: sa.Engine) -> None:
    """Seed tenant B as the schema owner so its lane has a valid context."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, display_name, primary_currency) "
                "VALUES (:id, 'rotana', 'Rotana', 'USD') ON CONFLICT DO NOTHING"
            ),
            {"id": TENANT_B},
        )


def _purge_test_rows(engine: sa.Engine) -> None:
    """Remove this module's group/channel rows for every tenant.

    Runs as the ``postgres`` superuser connection, which bypasses RLS, so the
    purge reaches rows written under either tenant lane and reruns stay
    idempotent on the shared clean-room container.
    """
    groups = [GROUP_ID, SECOND_GROUP_ID, OTHER_GROUP_ID]
    channels = [CHANNEL_ID, SECOND_ID]
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM channel_group_members WHERE group_id IN "
                "(SELECT id FROM channel_groups WHERE cms_group_id = ANY(:groups))"
            ),
            {"groups": groups},
        )
        conn.execute(
            sa.text(
                "DELETE FROM channel_group_members WHERE channel_id IN "
                "(SELECT id FROM youtube_channels WHERE youtube_channel_id = ANY(:ids))"
            ),
            {"ids": channels},
        )
        conn.execute(
            sa.text("DELETE FROM channel_groups WHERE cms_group_id = ANY(:groups)"),
            {"groups": groups},
        )
        conn.execute(
            sa.text("DELETE FROM youtube_channels WHERE youtube_channel_id = ANY(:ids)"),
            {"ids": channels},
        )


def _seed_channel(engine: sa.Engine, youtube_channel_id: str, channel_name: str) -> None:
    """Insert one INSIDE_CMS channel for tenant A through the owner engine.

    The sync route never creates channels (unknown CMS members are surfaced,
    not invented), so a member must already exist in ``youtube_channels`` for
    the mirror to attach it to a group.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO youtube_channels "
                "(id, tenant_id, youtube_channel_id, channel_name, cms_status, "
                " content_owner_id, revenue_required, revenue_source_status, active) "
                "VALUES (gen_random_uuid(), :tenant, :cid, :name, 'INSIDE_CMS', "
                " :owner, FALSE, 'PERFORMANCE_ONLY', TRUE)"
            ),
            {
                "tenant": TENANT_A,
                "cid": youtube_channel_id,
                "name": channel_name,
                "owner": CONTENT_OWNER,
            },
        )


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL, migrated to head with tenant B."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    engine = sa.create_engine(url)
    try:
        _seed_isolation_tenant(engine)
    finally:
        engine.dispose()
    return url


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an RLS-bypassing owner engine, purging test rows either side."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_test_rows(engine)
        yield engine
        _purge_test_rows(engine)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def fake_credentials() -> Iterator[MagicMock]:
    """Patch credential resolution at the route module for every test.

    Same substitution point as the SQLite tier: no test here touches the
    network or a real ``api_connector_credentials`` row.
    """
    with patch(
        "ums_smart_revenue.api.channels.resolve_connector_credentials",
        return_value=MagicMock(name="credentials"),
    ) as resolver:
        yield resolver


class FakeGroupsClient:
    """Canned stand-in for YouTubeGroupsClient; never performs any I/O."""

    def __init__(self, snapshot: list[tuple[str, str, tuple[str, ...]]]) -> None:
        """Hold the canned (key, title, member ids) snapshot."""
        self._snapshot = snapshot

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list for the requested content owner."""
        assert account_id == CONTENT_OWNER
        return [CmsGroup(cms_group_id=key, title=title) for key, title, _ids in self._snapshot]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group."""
        assert account_id == CONTENT_OWNER
        for key, _title, channel_ids in self._snapshot:
            if key == group_id:
                return CmsGroupMembers(channel_ids=channel_ids, non_channel_count=0)
        raise AssertionError(f"unexpected group_id: {group_id}")

    def close(self) -> None:
        """No-op: this double never opens a real HTTP client."""


def auth_headers() -> dict[str, str]:
    """Build global super-owner headers accepted by the trusted gateway."""
    return {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": "super_owner",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_sync_app(pg_url: str, client_double: FakeGroupsClient):
    """Build a Postgres-backed app whose groups client is the canned double."""
    app = create_app(database_url=pg_url, authz_source="headers")
    app.dependency_overrides[current_groups_client_factory] = lambda: (
        lambda _credentials: client_double
    )
    return app


def post_sync(client: TestClient, **overrides: object):
    """POST one apply-mode sync request as a global super owner."""
    body: dict[str, object] = {
        "content_owner_id": CONTENT_OWNER,
        "dry_run": False,
        "reason": "Mirror CMS grouping",
    }
    body.update(overrides)
    return client.post(SYNC_URL, headers=auth_headers(), json=body)


def _rows_in_tenant_lane(
    url: str, tenant_id: str, slug: str, statement: str, params: dict[str, object] | None = None
) -> list[sa.Row]:
    """Run one statement through a tenant lane session and return its rows.

    The lane session runs as ``app_tenant`` with the trusted tenant context
    row set by the after_begin hook, so RLS applies exactly as it does for a
    request handler.
    """
    factory = build_session_factory(url)
    token = TENANT_CTX.set(_tenant(tenant_id, slug))
    try:
        with factory() as session:
            return list(session.execute(sa.text(statement), params or {}).all())
    finally:
        TENANT_CTX.reset(token)


def _synced_group_keys_visible_to_tenant(url: str, tenant_id: str, slug: str) -> set[str]:
    """Return the CMS group keys one tenant lane can see with NO tenant filter.

    The ``SELECT`` deliberately omits ``WHERE tenant_id`` so the only thing
    that can hide a row is the ``channel_groups`` RLS policy, not the
    application-level filter in ``SqlAlchemyChannelGroupRegistry``.
    """
    rows = _rows_in_tenant_lane(url, tenant_id, slug, "SELECT cms_group_id FROM channel_groups")
    return {row.cms_group_id for row in rows if row.cms_group_id is not None}


def _group_row(engine: sa.Engine, cms_group_id: str) -> sa.Row | None:
    """Return one group's stored columns through the RLS-bypassing owner engine."""
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT tenant_id, name, group_type, active FROM channel_groups "
                "WHERE cms_group_id = :key"
            ),
            {"key": cms_group_id},
        ).first()


def _audit_log_count(engine: sa.Engine) -> int:
    """Count every audit_logs row through the RLS-bypassing owner engine."""
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT COUNT(*) FROM audit_logs")).scalar_one()


def _tenant_audit_log_count(engine: sa.Engine, tenant_id: str) -> int:
    """Count one tenant's audit_logs rows through the owner engine.

    The in-flight probe runs on the audit session, which is app_platform —
    NOBYPASSRLS — so its bare COUNT(*) is filtered to the request's tenant.
    The anti-vacuity baseline therefore has to be tenant-scoped too.
    """
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT COUNT(*) FROM audit_logs WHERE tenant_id = :tenant"),
            {"tenant": tenant_id},
        ).scalar_one()


class _FailingGroupStore:
    """Group store that delegates until the Nth ``create_group`` call.

    Used to force a failure AFTER the sync has already written one group, its
    membership, and that group's GROUP_UPDATED audit row, which is the only
    way to exercise the mid-apply rollback path (every input-validation
    rejection happens before any write).
    """

    def __init__(self, inner: SqlAlchemyChannelGroupRegistry, *, fail_on_call: int) -> None:
        """Wrap the real store and arm the failure for one creation call."""
        self._inner = inner
        self._fail_on_call = fail_on_call
        self.create_calls = 0

    def list_synced_groups(self, *, content_owner_id: str | None = None) -> list[ChannelGroupEntry]:
        """Delegate the CMS-keyed group listing to the real store."""
        return self._inner.list_synced_groups(content_owner_id=content_owner_id)

    def get_group(self, group_id: str, *, for_update: bool = False) -> ChannelGroupEntry | None:
        """Delegate the group lookup, keeping the store's row-lock option."""
        return self._inner.get_group(group_id, for_update=for_update)

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Delegate the CMS-key lookup to the real store."""
        return self._inner.get_group_by_cms_id(cms_group_id, for_update=for_update)

    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Delegate the bulk archived-key lookup to the real store."""
        return self._inner.list_archived_cms_group_ids(cms_group_ids)

    def list_foreign_owner_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Delegate the bulk cross-owner-key lookup to the real store."""
        return self._inner.list_foreign_owner_cms_group_ids(
            cms_group_ids, content_owner_id=content_owner_id
        )

    def create_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate group creation until the armed call, then raise."""
        self.create_calls += 1
        if self.create_calls >= self._fail_on_call:
            raise RuntimeError("channel group store unavailable mid-apply")
        return self._inner.create_group(**kwargs)

    def update_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate name/active updates to the real store."""
        return self._inner.update_group(**kwargs)

    def add_members(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate membership attachment to the real store."""
        return self._inner.add_members(**kwargs)

    def remove_member(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate membership detachment to the real store."""
        return self._inner.remove_member(**kwargs)


OTHER_CONTENT_OWNER = "OtherOwnerBBBBBBBBBBBB"
OTHER_GROUP_ID = "cms-owner-b"


def _seed_other_owner_group(engine: sa.Engine, *, cms_group_id: str, content_owner_id: str) -> None:
    """Insert one CMS-keyed group for a DIFFERENT owner, active, no membership.

    Models a group a prior sync already created for another content owner —
    the real-SQL counterpart of the in-memory P1 regression in
    ``test_channel_group_sync_api.py``.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO channel_groups"
                " (tenant_id, name, group_type, cms_group_id, content_owner_id)"
                " VALUES (:tenant, 'Owner B Sector', 'SECTOR', :cms_group_id, :owner)"
            ),
            {"tenant": TENANT_A, "cms_group_id": cms_group_id, "owner": content_owner_id},
        )


def test_create_stamps_content_owner_id_on_postgres(pg_url: str, owner_engine: sa.Engine) -> None:
    """An applied CREATE stamps the group's content_owner_id in SQL."""
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    fake = FakeGroupsClient([(GROUP_ID, GROUP_TITLE, (CHANNEL_ID,))])
    client = TestClient(build_sync_app(pg_url, fake))

    response = post_sync(client)

    assert response.status_code == 200, response.text
    with owner_engine.connect() as conn:
        stored = conn.execute(
            sa.text("SELECT content_owner_id FROM channel_groups WHERE cms_group_id = :key"),
            {"key": GROUP_ID},
        ).one()
    assert stored.content_owner_id == CONTENT_OWNER


def test_sync_does_not_deactivate_a_different_owners_group_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """Syncing owner A's real SQL groups must not touch owner B's group.

    list_synced_groups() must be scoped to the requesting content owner in
    the SQL implementation too, not only the in-memory fake: without the
    WHERE content_owner_id filter, owner B's group is simply missing from
    owner A's upstream snapshot and gets planned as DEACTIVATE.
    """
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    _seed_other_owner_group(
        owner_engine, cms_group_id=OTHER_GROUP_ID, content_owner_id=OTHER_CONTENT_OWNER
    )
    fake = FakeGroupsClient([(GROUP_ID, GROUP_TITLE, (CHANNEL_ID,))])
    client = TestClient(build_sync_app(pg_url, fake))

    response = post_sync(client)

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["DEACTIVATE"] == 0
    other_owner_group = _group_row(owner_engine, OTHER_GROUP_ID)
    assert other_owner_group is not None
    assert other_owner_group.active is True


def test_sync_persists_groups_on_postgres(pg_url: str, owner_engine: sa.Engine) -> None:
    """An applied CREATE lands the group row and its membership on Postgres."""
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    fake = FakeGroupsClient([(GROUP_ID, GROUP_TITLE, (CHANNEL_ID,))])
    client = TestClient(build_sync_app(pg_url, fake))

    response = post_sync(client)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is False
    assert payload["counts"]["CREATE"] == 1
    assert payload["unknown_channel_total"] == 0

    # The tenant lane (app_tenant + trusted tenant context) must see the group.
    groups = _rows_in_tenant_lane(
        pg_url,
        TENANT_A,
        "ums",
        "SELECT cms_group_id, name, group_type, active FROM channel_groups "
        "WHERE cms_group_id = :key",
        {"key": GROUP_ID},
    )
    assert len(groups) == 1, groups
    stored = groups[0]
    assert stored.cms_group_id == GROUP_ID
    assert stored.name == GROUP_TITLE
    assert stored.group_type == "SECTOR"
    assert stored.active is True

    members = _rows_in_tenant_lane(
        pg_url,
        TENANT_A,
        "ums",
        "SELECT c.youtube_channel_id FROM channel_group_members m "
        "JOIN channel_groups g ON g.id = m.group_id AND g.tenant_id = m.tenant_id "
        "JOIN youtube_channels c ON c.id = m.channel_id AND c.tenant_id = m.tenant_id "
        "WHERE g.cms_group_id = :key",
        {"key": GROUP_ID},
    )
    assert [row.youtube_channel_id for row in members] == [CHANNEL_ID]


def test_synced_groups_are_tenant_isolated(pg_url: str, owner_engine: sa.Engine) -> None:
    """A group mirrored by tenant A is invisible to tenant B's lane."""
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    fake = FakeGroupsClient([(GROUP_ID, GROUP_TITLE, (CHANNEL_ID,))])
    client = TestClient(build_sync_app(pg_url, fake))

    response = post_sync(client)
    assert response.status_code == 200, response.text

    # Database boundary: bare SELECT, no WHERE tenant_id — RLS is the filter.
    assert GROUP_ID in _synced_group_keys_visible_to_tenant(pg_url, TENANT_A, "ums")
    assert GROUP_ID not in _synced_group_keys_visible_to_tenant(pg_url, TENANT_B, "rotana")

    # Control: the row really was written for tenant A, so B's absence is real.
    stored = _group_row(owner_engine, GROUP_ID)
    assert stored is not None
    assert str(stored.tenant_id) == TENANT_A


def test_mid_apply_failure_rolls_back_groups_and_audit_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """A failure after the first group rolls back groups AND audit rows.

    The armed store raises on the SECOND ``create_group``, so by the time the
    request fails the first group row, its membership, and its GROUP_UPDATED
    audit row have all been written. Nothing may survive: a durable
    GROUP_UPDATED for a group that does not exist would make the audit trail
    claim a mirror that never happened.

    ``audit_counts_in_flight`` is the anti-vacuity guard. It records what the
    audit session itself sees right after each flushed INSERT (a transaction
    always sees its own uncommitted rows), so the test proves the audit row
    physically existed before the failure — otherwise "no audit rows
    afterwards" would be trivially true.
    """
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    _seed_channel(owner_engine, SECOND_ID, "Beta Sports")
    fake = FakeGroupsClient(
        [
            (GROUP_ID, GROUP_TITLE, (CHANNEL_ID,)),
            (SECOND_GROUP_ID, "Sports Sector", (SECOND_ID,)),
        ]
    )
    app = build_sync_app(pg_url, fake)
    stores: list[_FailingGroupStore] = []
    audit_counts_in_flight: list[int] = []
    original_append = SqlAlchemyAuditSink.append

    def recording_append(sink: SqlAlchemyAuditSink, record: AuditRecord) -> None:
        """Append through the real sink, then read the in-transaction count."""
        original_append(sink, record)
        audit_counts_in_flight.append(
            sink._session.execute(  # noqa: SLF001 — test probe of the audit lane
                sa.text("SELECT COUNT(*) FROM audit_logs")
            ).scalar_one()
        )

    def failing_group_store(
        session: Annotated[Session, Depends(current_db_session)],
    ) -> _FailingGroupStore:
        """Provide a group store armed to fail on the second group's CREATE."""
        store = _FailingGroupStore(SqlAlchemyChannelGroupRegistry(session), fail_on_call=2)
        stores.append(store)
        return store

    app.dependency_overrides[sql_group_registry_from_session] = failing_group_store
    failing_client = TestClient(app, raise_server_exceptions=False)
    before = _audit_log_count(owner_engine)
    before_tenant = _tenant_audit_log_count(owner_engine, TENANT_A)

    with patch.object(SqlAlchemyAuditSink, "append", recording_append):
        response = post_sync(failing_client)

    assert response.status_code == 500, response.text
    assert stores and stores[0].create_calls == 2, "the store must fail mid-apply, not before it"
    # One audit row was really INSERTed before the failure: the first group's
    # GROUP_UPDATED. The request did not fail ahead of the audit write.
    assert audit_counts_in_flight == [before_tenant + 1]
    assert _group_row(owner_engine, GROUP_ID) is None
    assert _group_row(owner_engine, SECOND_GROUP_ID) is None
    assert _audit_log_count(owner_engine) == before


def test_tenant_commit_failure_persists_no_audit_rows_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """Audit rows share the tenant transaction's fate: no commit, no audit.

    The mid-apply test above only covers the EXCEPTION path, where FastAPI
    throws into both yield-dependencies and both sessions roll back. This is
    the commit-order path the bulk import already closes (see
    ``tests/api/test_channels_import_postgres.py``): the handler returns
    success and only then does the tenant transaction fail to persist — a
    serialization or connection error at commit time, simulated here by a
    dependency that rolls back where the wired dependency would commit.

    Because the sync's audit rows must join the SAME transaction as the group
    writes, they have to vanish with them. On the app-wide sink they did not:
    that sink runs on the independently committed platform session, which
    FastAPI tears down BEFORE the tenant session, leaving durable
    GROUP_UPDATED + GROUPS_SYNCED rows describing a mirror that never happened.
    """
    _seed_channel(owner_engine, CHANNEL_ID, "Alpha News")
    fake = FakeGroupsClient([(GROUP_ID, GROUP_TITLE, (CHANNEL_ID,))])
    app = build_sync_app(pg_url, fake)
    factory = build_session_factory(pg_url)

    def rollback_instead_of_commit() -> Iterator[Session]:
        """Yield a real tenant-lane session but never let it commit."""
        with factory() as session:
            try:
                yield session
            finally:
                session.rollback()

    app.dependency_overrides[current_db_session] = rollback_instead_of_commit
    client = TestClient(app)
    before_tenant = _tenant_audit_log_count(owner_engine, TENANT_A)

    response = post_sync(client)

    # Anti-vacuity: the handler itself succeeded and really applied a CREATE,
    # so both audit events were emitted. Only the commit was lost.
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["CREATE"] == 1
    assert _group_row(owner_engine, GROUP_ID) is None
    assert _tenant_audit_log_count(owner_engine, TENANT_A) == before_tenant
