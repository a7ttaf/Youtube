# ============================================================================
# Purpose: Prove at the Postgres tier the four claims the owner-stamp
#   recovery route (DELETE /groups/{group_id}/content-owner) makes and which
#   SQLite cannot check: the clear is durable and really returns the group to
#   the adoptable pool on the real engine; its FOR NO KEY UPDATE row lock
#   actually serializes a concurrent adopt; the audited previous owner comes
#   from that locked read rather than the route's unlocked pre-read; and its
#   audit row shares the tenant transaction's fate.
# Database/ORM: Real PostgreSQL via UMS_TEST_DATABASE_URL. require_postgres_url
#   RAISES rather than skipping, so a missing container fails the suite loudly
#   instead of quietly deleting this coverage.
# Standards: App construction, tenant context, the owner-engine purge, and the
#   lost-commit mechanism are reused verbatim from
#   tests/api/test_channel_group_sync_postgres.py so the Postgres-tier files
#   stay one shape. The serialization proof is a REAL two-session race, never a
#   store double: SQLite ignores FOR UPDATE entirely (single writer), so this
#   is the only tier on which that claim can fail. Blocking is proven by
#   PostgreSQL's own pg_blocking_pids() rather than by sleeping and hoping;
#   wall-clock ordering is asserted too, but only as corroboration.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/api/groups.py -> subject (the route).
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
#     clear_content_owner, the row-locked write these tests exercise.
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py ->
#     _execute_update, the locked-re-read adopt path test 2 replays.
# ============================================================================
"""Postgres-tier proof for DELETE /groups/{id}/content-owner.

The SQLite tier (``tests/api/test_groups_api.py``) already pins who may call
the route, which states it refuses, and that a cleared group is re-adopted by
the right owner's next sync. Four of its promises are not decidable there:

1. **Durability of the recovery loop on real SQL.** SQLite's store shares one
   session per app, so "the clear persisted and a later sync re-stamped it"
   never crosses a real commit boundary.
2. **Serialization against a concurrent adopt.** ``clear_content_owner``
   documents a FOR NO KEY UPDATE row lock so that "a concurrent adopt
   serializes against this clear". SQLite ignores locking clauses entirely, so
   that sentence is unfalsifiable there and only means something here.
3. **The audit names what the LOCKED write erased.** The route pre-reads the
   group unlocked (it needs the 404 before taking a write lock), so an adopt
   committing in that window makes the pre-read's ``content_owner_id`` stale.
   Staging that interleaving needs two committed sessions and READ COMMITTED
   re-reads under a lock — neither exists on SQLite.
4. **All-or-nothing including audit.** The route writes a domain row and an
   audit row in one request through ``current_atomic_audit_sink``. A tenant
   commit that fails to persist must take the ``GROUP_UPDATED`` row with it,
   or the trail claims a clear that never landed (#169 invariant).

``require_postgres_url()`` raises (never skips) when ``UMS_TEST_DATABASE_URL``
is unset, preserving the repository's no-skip policy.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
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

CHANNEL_ID = "UCzT9gT0mOfIYHrGpqXaMvSA"
GROUP_KEY = "cms-owner-stamp-recovery"
GROUP_TITLE = "Recovery Sector"
# Real CMS content owner ids are ~22 characters; the route caps the length.
CONTENT_OWNER_WRONG = "WrongOwnerDDDDDDDDDDDD"
CONTENT_OWNER_RIGHT = "RightOwnerEEEEEEEEEEEE"
SYNC_URL = "/channels/groups/sync"
CLEAR_REASON = "Stamped to the wrong content owner during migration"

# Tenant A is the bootstrap UMS tenant every trusted-header request binds to.
TENANT_A = UMS_TENANT_ID

# The lock proof polls PostgreSQL for the waiter's blocked state instead of
# sleeping a fixed amount and assuming. The deadline is generous because it is
# only ever reached when the test is about to FAIL.
LOCK_POLL_SECONDS = 0.02
LOCK_DEADLINE_SECONDS = 10.0

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
    """Build an ACTIVE tenant object for lane-scoped reads."""
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


def _purge_test_rows(engine: sa.Engine) -> None:
    """Remove this module's group/channel rows for every tenant.

    Runs as the ``postgres`` superuser connection, which bypasses RLS, so the
    purge reaches rows written under any tenant lane and reruns stay idempotent
    on the shared clean-room container.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM channel_group_members WHERE group_id IN "
                "(SELECT id FROM channel_groups WHERE cms_group_id = :key)"
            ),
            {"key": GROUP_KEY},
        )
        conn.execute(
            sa.text(
                "DELETE FROM channel_group_members WHERE channel_id IN "
                "(SELECT id FROM youtube_channels WHERE youtube_channel_id = :cid)"
            ),
            {"cid": CHANNEL_ID},
        )
        conn.execute(
            sa.text("DELETE FROM channel_groups WHERE cms_group_id = :key"),
            {"key": GROUP_KEY},
        )
        conn.execute(
            sa.text("DELETE FROM youtube_channels WHERE youtube_channel_id = :cid"),
            {"cid": CHANNEL_ID},
        )


def _seed_channel(engine: sa.Engine, *, content_owner_id: str) -> None:
    """Insert one INSIDE_CMS channel for tenant A through the owner engine.

    The sync never creates channels (unknown CMS members are surfaced, not
    invented), so a member must already exist for the mirror to keep it
    attached. The stamp matters: ``_known_member_channel_ids`` excludes a
    channel owned by a DIFFERENT content owner, which would turn this member
    into an unknown id and plan a membership removal.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO youtube_channels "
                "(id, tenant_id, youtube_channel_id, channel_name, cms_status, "
                " content_owner_id, revenue_required, revenue_source_status, active) "
                "VALUES (gen_random_uuid(), :tenant, :cid, 'Recovery News', 'INSIDE_CMS', "
                " :owner, FALSE, 'PERFORMANCE_ONLY', TRUE)"
            ),
            {"tenant": TENANT_A, "cid": CHANNEL_ID, "owner": content_owner_id},
        )


def _seed_stamped_group(
    engine: sa.Engine, *, content_owner_id: str | None, channel_ids: tuple[str, ...] = ()
) -> str:
    """Create one CMS-keyed group stamped to ``content_owner_id``; return its id.

    Written through the real store rather than raw SQL so the row is shaped
    exactly like one a sync would have produced — the mis-stamped state this
    route exists to undo.
    """
    with Session(engine) as session:
        store = SqlAlchemyChannelGroupRegistry(session)
        group = store.create_group(
            name=GROUP_TITLE,
            group_type="SECTOR",
            channel_ids=list(channel_ids),
            cms_group_id=GROUP_KEY,
            content_owner_id=content_owner_id,
        )
        session.commit()
        return group.id


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL, migrated to head."""
    url = require_postgres_url()
    _ensure_upgraded(url)
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
    """Patch credential resolution at the sync route module for every test.

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
        """Hold the canned (cms key, title, member ids) snapshot."""
        self._snapshot = snapshot

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list for the requested content owner."""
        assert account_id == CONTENT_OWNER_RIGHT
        return [CmsGroup(cms_group_id=key, title=title) for key, title, _ids in self._snapshot]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group."""
        assert account_id == CONTENT_OWNER_RIGHT
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


def build_recovery_app(pg_url: str, client_double: FakeGroupsClient | None = None):
    """Build a Postgres-backed app, optionally with the canned groups client."""
    app = create_app(database_url=pg_url, authz_source="headers")
    if client_double is not None:
        app.dependency_overrides[current_groups_client_factory] = lambda: (
            lambda _credentials: client_double
        )
    return app


def clear_stamp(client: TestClient, group_id: str, *, reason: str = CLEAR_REASON):
    """DELETE one group's content-owner stamp as a global super owner."""
    return client.delete(
        f"/groups/{group_id}/content-owner",
        headers=auth_headers(),
        params={"reason": reason},
    )


def post_sync(client: TestClient, *, content_owner_id: str = CONTENT_OWNER_RIGHT):
    """POST one apply-mode sync request as a global super owner."""
    return client.post(
        SYNC_URL,
        headers=auth_headers(),
        json={
            "content_owner_id": content_owner_id,
            "dry_run": False,
            "reason": "Re-adopt the group under its real content owner",
        },
    )


def _group_row(engine: sa.Engine) -> sa.Row | None:
    """Return the test group's stored columns through the owner engine."""
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT tenant_id, name, active, content_owner_id FROM channel_groups "
                "WHERE cms_group_id = :key"
            ),
            {"key": GROUP_KEY},
        ).first()


def _group_row_in_tenant_lane(url: str) -> sa.Row | None:
    """Return the test group's columns as tenant A's own lane sees them.

    The lane session runs as ``app_tenant`` with the trusted tenant context row
    set by the after_begin hook, so RLS applies exactly as it does for a
    request handler — this is the read an operator's next page load performs,
    not a superuser peek.
    """
    factory = build_session_factory(url)
    token = TENANT_CTX.set(_tenant(TENANT_A, "ums"))
    try:
        with factory() as session:
            return session.execute(
                sa.text(
                    "SELECT name, active, cms_group_id, content_owner_id "
                    "FROM channel_groups WHERE cms_group_id = :key"
                ),
                {"key": GROUP_KEY},
            ).first()
    finally:
        TENANT_CTX.reset(token)


def _tenant_audit_log_count(engine: sa.Engine, tenant_id: str) -> int:
    """Count one tenant's audit_logs rows through the owner engine.

    Tenant-scoped rather than global: the in-flight probe runs on the request's
    own session, which is app_platform (NOBYPASSRLS), so its bare COUNT(*) is
    filtered to the request's tenant. The baseline has to match.
    """
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT COUNT(*) FROM audit_logs WHERE tenant_id = :tenant"),
            {"tenant": tenant_id},
        ).scalar_one()


def test_clear_persists_and_group_is_readoptable_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """The recovery loop on the real engine: clear commits, the right owner adopts.

    Adoption is one-way in every other writer, so a group stamped to the wrong
    content owner is governed by that owner's sync forever unless the clear
    both PERSISTS and leaves the row otherwise untouched. The tenant-lane read
    is the operator's own view (app_tenant + RLS), not a superuser peek, and it
    checks the three fields the route must not have moved alongside the one it
    must have.
    """
    _seed_channel(owner_engine, content_owner_id=CONTENT_OWNER_RIGHT)
    group_id = _seed_stamped_group(
        owner_engine, content_owner_id=CONTENT_OWNER_WRONG, channel_ids=(CHANNEL_ID,)
    )
    fake = FakeGroupsClient([(GROUP_KEY, GROUP_TITLE, (CHANNEL_ID,))])
    client = TestClient(build_recovery_app(pg_url, fake))
    before_audit = _tenant_audit_log_count(owner_engine, TENANT_A)

    cleared = clear_stamp(client, group_id)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["content_owner_id"] is None
    # Durable, and only the stamp moved: name, active state and CMS key are the
    # group's identity to every later sync, so a clear that disturbed them
    # would trade one wrong mirror for another.
    after_clear = _group_row_in_tenant_lane(pg_url)
    assert after_clear is not None, "the cleared group vanished from its own tenant lane"
    assert after_clear.content_owner_id is None
    assert after_clear.name == GROUP_TITLE
    assert after_clear.active is True
    assert after_clear.cms_group_id == GROUP_KEY
    # The committed path really writes the GROUP_UPDATED row. This is the
    # contrast partner for the lost-commit test below, whose "no audit rows"
    # assertion would otherwise be trivially true.
    assert _tenant_audit_log_count(owner_engine, TENANT_A) == before_audit + 1

    synced = post_sync(client)

    assert synced.status_code == 200, synced.text
    payload = synced.json()
    # Anti-vacuity: the apply reports adoption from the WRITE boundary, so this
    # is the sync stating it performed the re-stamp, not the plan predicting it.
    entries = [group for group in payload["groups"] if group["cms_group_id"] == GROUP_KEY]
    assert len(entries) == 1, payload["groups"]
    assert entries[0]["will_adopt_content_owner"] is True
    stored = _group_row(owner_engine)
    assert stored is not None
    assert stored.content_owner_id == CONTENT_OWNER_RIGHT


def _adopt_under_lock(
    engine: sa.Engine,
    *,
    group_id: str,
    owner: str,
    started: threading.Event,
    observed: dict[str, object],
    errors: list[Exception],
) -> None:
    """Replay ``_execute_update``'s adopt on a second session, recording timings.

    Deliberately the same two store calls, in the same order, that the sync
    apply makes: the locked re-read that re-verifies the scoping premise, then
    the adopt-only ``update_group``. Reading with ``for_update=True`` is what
    makes this serialize; the store's ``update_group`` alone reads UNLOCKED and
    would evaluate ``require_adoptable_owner`` against a stale snapshot.
    """
    session = Session(engine)
    try:
        observed["pid"] = session.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        store = SqlAlchemyChannelGroupRegistry(session)
        started.set()
        observed["attempted_at"] = time.monotonic()
        current = store.get_group(group_id, for_update=True)
        observed["unblocked_at"] = time.monotonic()
        if current is None:
            raise AssertionError(f"group vanished before the adopt: {group_id}")
        observed["owner_seen_under_lock"] = current.content_owner_id
        store.update_group(group_id=group_id, name=None, active=None, content_owner_id=owner)
        session.commit()
    except Exception as exc:  # re-raised in the main thread, which owns the assertions
        errors.append(exc)
        session.rollback()
    finally:
        session.close()


def _blocking_pids(engine: sa.Engine, pid: int) -> list[int]:
    """Return the backend pids PostgreSQL reports as blocking ``pid``."""
    with engine.connect() as conn:
        return list(
            conn.execute(sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}).scalar_one()
        )


def _await_blocked_by(engine: sa.Engine, pid: int, *, blocker: int) -> None:
    """Wait until ``pid`` is parked behind ``blocker``; fail loudly if it never is.

    Polling the server's own lock view is what makes this proof deterministic:
    a sleep-then-assume test would pass just as happily if the waiter had
    sailed straight through without ever taking the lock.
    """
    deadline = time.monotonic() + LOCK_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if blocker in _blocking_pids(engine, pid):
            return
        time.sleep(LOCK_POLL_SECONDS)
    raise AssertionError(
        f"backend {pid} never blocked on {blocker}: the adopt did not serialize "
        "against the uncommitted clear"
    )


def test_clear_serializes_against_concurrent_adopt_on_postgres(owner_engine: sa.Engine) -> None:
    """A concurrent adopt waits for the clear's row lock, then sees the cleared value.

    Session A runs ``clear_content_owner`` and HOLDS its transaction open, so
    it owns the group's FOR NO KEY UPDATE lock. Session B replays the sync
    apply's adopt (locked re-read, then ``update_group``) on the same row.

    Two things are proven, and the second is the one that matters. First, B
    genuinely blocks — asserted from ``pg_blocking_pids()``, PostgreSQL's own
    account of who is waiting on whom, not from elapsed time. Second, once A
    commits, B's locked re-read returns the CLEARED row rather than its
    pre-lock snapshot: under READ COMMITTED the locking clause re-reads the
    updated tuple, which is precisely why the adopt then succeeds instead of
    tripping ``require_adoptable_owner`` on the stale wrong owner.

    The end state asserted is the one this exact commit order produces: A's
    clear landed, and B's adopt then stamped B's owner onto the freed row.
    Both writes are legitimate; the point is that they happened in series, not
    that either was refused.
    """
    group_id = _seed_stamped_group(owner_engine, content_owner_id=CONTENT_OWNER_WRONG)
    holder = Session(owner_engine)
    started = threading.Event()
    observed: dict[str, object] = {}
    errors: list[Exception] = []
    adopter = threading.Thread(
        target=_adopt_under_lock,
        kwargs={
            "engine": owner_engine,
            "group_id": group_id,
            "owner": CONTENT_OWNER_RIGHT,
            "started": started,
            "observed": observed,
            "errors": errors,
        },
        name="concurrent-adopt",
    )
    try:
        holder_pid = holder.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        holder_store = SqlAlchemyChannelGroupRegistry(holder)
        cleared = holder_store.clear_content_owner(group_id=group_id)
        # A now holds the row lock with the clear flushed but NOT committed.
        assert cleared.group.content_owner_id is None
        assert cleared.previous_content_owner_id == CONTENT_OWNER_WRONG

        adopter.start()
        assert started.wait(timeout=LOCK_DEADLINE_SECONDS), "the adopt thread never started"
        _await_blocked_by(owner_engine, int(observed["pid"]), blocker=holder_pid)

        released_at = time.monotonic()
        holder.commit()

        adopter.join(timeout=LOCK_DEADLINE_SECONDS)
        assert not adopter.is_alive(), "the adopt never unblocked after the clear committed"
        if errors:
            raise errors[0]
    finally:
        holder.rollback()
        holder.close()
        if adopter.ident is not None:
            adopter.join(timeout=LOCK_DEADLINE_SECONDS)

    # Corroborating wall clock: B queued its locked read BEFORE the release and
    # only got through it AFTER. On its own this would be weak; behind the
    # pg_blocking_pids assertion it just confirms the same ordering twice.
    assert observed["attempted_at"] < released_at
    assert observed["unblocked_at"] >= released_at
    # The lock's whole purpose: B's re-read observes the clear, so the group is
    # adoptable to it. Had it read the pre-lock snapshot it would have seen the
    # wrong owner and refused the adopt.
    assert observed["owner_seen_under_lock"] is None
    stored = _group_row(owner_engine)
    assert stored is not None
    assert stored.content_owner_id == CONTENT_OWNER_RIGHT


class _AdoptAtPreReadRegistry(SqlAlchemyChannelGroupRegistry):
    """The real store, with one concurrent adopt committed at the pre-read seam.

    NOT a store double: every read and write below is the production store's.
    The subclass only sequences a second session to land its adopt in the exact
    window the route leaves open — after the unlocked ``get_group`` pre-read
    and before ``clear_content_owner`` takes its row lock. That window is
    microseconds wide in production and cannot be hit reliably by racing
    threads, so it is opened deterministically here instead.
    """

    def __init__(self, session: Session, *, adopt_engine: sa.Engine, owner: str) -> None:
        """Wrap the real store with the engine and owner the interloper uses."""
        super().__init__(session)
        self._adopt_engine = adopt_engine
        self._adopt_owner = owner
        self._adopted = False

    def get_group(self, group_id: str, *, for_update: bool = False) -> ChannelGroupEntry | None:
        """Run the real pre-read, then let a committed adopt invalidate it once."""
        entry = super().get_group(group_id, for_update=for_update)
        if not self._adopted and not for_update:
            self._adopted = True
            with Session(self._adopt_engine) as adopter:
                SqlAlchemyChannelGroupRegistry(adopter).update_group(
                    group_id=group_id,
                    name=None,
                    active=None,
                    content_owner_id=self._adopt_owner,
                )
                adopter.commit()
        return entry


def _clear_audit_details(engine: sa.Engine, group_id: str) -> dict:
    """Return the one clear-stamp audit row's details for ``group_id``.

    Scoped by entity_id: ``_purge_test_rows`` clears group and channel rows
    between tests but deliberately leaves audit_logs alone, so a module-wide
    query would also match earlier tests' clears.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT details FROM audit_logs WHERE tenant_id = :tenant "
                "AND event_type = 'GROUP_UPDATED' AND entity_id = :group_id "
                "AND details->>'action' = 'content_owner_cleared'"
            ),
            {"tenant": TENANT_A, "group_id": group_id},
        ).all()
    assert len(rows) == 1, f"expected exactly one clear audit row, got {len(rows)}"
    return rows[0].details


def test_clear_audits_the_owner_read_under_the_lock_not_the_pre_read(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """The audit row names the owner the LOCKED write erased, not a stale pre-read.

    The route pre-reads the group unlocked (it needs the 404 before taking a
    write lock). Only ``clear_content_owner``'s ``FOR NO KEY UPDATE`` read is
    serialized against a concurrent adopt, so the pre-read's ``content_owner_id``
    is not a safe source for ``previous_content_owner_id``.

    The ordering staged here is the one that breaks it: the group is owner-NULL
    when the route pre-reads it, the correct owner's sync adopts and commits,
    and only then does the clear take its lock and erase a stamp that the
    pre-read never saw. Sourcing the audit detail from the pre-read yields
    ``None`` — a row claiming nothing was erased while a real owner stamp was.
    Sourcing it from under the lock yields the owner actually removed.

    Only PostgreSQL can stage this: under READ COMMITTED the locked re-read
    picks up the interloper's committed tuple, which is precisely the
    divergence being asserted. SQLite's single writer has no such window.
    """
    group_id = _seed_stamped_group(owner_engine, content_owner_id=None)
    seeded = _group_row(owner_engine)
    assert seeded is not None
    assert seeded.content_owner_id is None

    def _registry_adopting_at_pre_read(
        session: Session = Depends(current_db_session),
    ) -> _AdoptAtPreReadRegistry:
        return _AdoptAtPreReadRegistry(
            session, adopt_engine=owner_engine, owner=CONTENT_OWNER_WRONG
        )

    app = build_recovery_app(pg_url)
    app.dependency_overrides[sql_group_registry_from_session] = _registry_adopting_at_pre_read
    with TestClient(app) as client:
        response = clear_stamp(client, group_id)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["content_owner_id"] is None
    # Read from the persisted row, not the response: audit_record_to_api omits
    # details, and the durable trail is what an auditor actually reads.
    # Anti-vacuity: the interloper's adopt really landed, so there WAS a stamp
    # to erase and the pre-read's None was genuinely stale.
    details = _clear_audit_details(owner_engine, group_id)
    assert details["previous_content_owner_id"] == CONTENT_OWNER_WRONG
    final = _group_row(owner_engine)
    assert final is not None
    assert final.content_owner_id is None


def test_clear_route_lost_commit_persists_no_audit_rows_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """Audit rows share the clear's transaction fate: no commit, no audit.

    The commit-order path both the bulk import and the CMS group sync already
    close, applied to the new route: the handler returns success and only then
    does the tenant transaction fail to persist — a serialization or connection
    error at commit time, simulated here by a dependency that rolls back where
    the wired dependency would commit.

    This route wires ``current_atomic_audit_sink`` precisely so its
    ``GROUP_UPDATED`` row joins the same transaction as the stamp write. On the
    app-wide sink it would not: that sink runs on the independently committed
    platform session, which FastAPI tears down BEFORE the tenant session,
    leaving a durable row announcing that a wrong owner stamp was cleared while
    the stamp is still sitting on the group.

    ``audit_in_flight`` is the anti-vacuity guard. It records what the request's
    own session sees right after the flushed audit INSERT (a transaction always
    sees its own uncommitted rows), proving the row physically existed before
    the commit was lost — otherwise "no audit rows afterwards" would be
    trivially true.
    """
    group_id = _seed_stamped_group(owner_engine, content_owner_id=CONTENT_OWNER_WRONG)
    app = build_recovery_app(pg_url)
    factory = build_session_factory(pg_url)

    def rollback_instead_of_commit() -> Iterator[Session]:
        """Yield a real tenant-lane session but never let it commit."""
        with factory() as session:
            try:
                yield session
            finally:
                session.rollback()

    audit_in_flight: list[int] = []
    original_append = SqlAlchemyAuditSink.append

    def recording_append(sink: SqlAlchemyAuditSink, record: AuditRecord) -> None:
        """Append through the real sink, then read the in-transaction count.

        The count MUST be read through the sink's own session. Any other
        connection sits outside the uncommitted transaction and would report
        the baseline regardless of what the sink wrote, which would make this
        probe prove nothing.
        """
        original_append(sink, record)
        audit_in_flight.append(
            sink._session.execute(  # noqa: SLF001 — test probe of the audit lane
                sa.text("SELECT COUNT(*) FROM audit_logs")
            ).scalar_one()
        )

    app.dependency_overrides[current_db_session] = rollback_instead_of_commit
    client = TestClient(app)
    before_tenant = _tenant_audit_log_count(owner_engine, TENANT_A)

    with patch.object(SqlAlchemyAuditSink, "append", recording_append):
        response = clear_stamp(client, group_id, reason="Lost-commit clear attempt")

    # Anti-vacuity: the handler itself succeeded and really cleared the stamp
    # in its transaction, and really appended the audit row. Only the commit
    # was lost.
    assert response.status_code == 200, response.text
    assert response.json()["content_owner_id"] is None
    assert audit_in_flight == [before_tenant + 1]
    # Durable state: the stamp is exactly as it was, and no orphan
    # GROUP_UPDATED survives to claim it was cleared.
    stored = _group_row(owner_engine)
    assert stored is not None
    assert stored.content_owner_id == CONTENT_OWNER_WRONG
    assert _tenant_audit_log_count(owner_engine, TENANT_A) == before_tenant
