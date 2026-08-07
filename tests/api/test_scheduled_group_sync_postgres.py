# ============================================================================
# Purpose: Prove at the Postgres tier what SQLite cannot for the SCHEDULED CMS
#   group sync -- that GroupSyncScheduler.tick(), driving a real
#   ConnectorJobExecutor over the app_tenant RLS lane, converges each ACTIVE
#   tenant's active youtube-analytics credential into group + membership + audit
#   rows stamped with the RIGHT tenant_id, and that the worker's fresh-session
#   failure audit lands cross-lane. The two-tenant test is the FIRST real-
#   Postgres proof of Sched 3's shared-session per-tenant rollback (tenant_
#   context.py's "first statement of a transaction" RLS boundary): the SQLite
#   tier cannot enforce RLS, so it cannot catch a missing per-tenant rollback by
#   construction -- if that rollback were absent, tenant B's list_credentials
#   would run inside tenant A's still-open, A-pinned transaction and RLS would
#   hide B's credential, so B's job would never be submitted.
# Database/ORM: Real PostgreSQL via UMS_TEST_DATABASE_URL. require_postgres_url
#   RAISES (never skips). Assertions read back through an RLS-bypassing owner
#   engine so they are not themselves subject to the lane under test.
# Standards: A REAL executor (group_sync_client_factory=<fake>, max_workers=1)
#   and a REAL scheduler over build_session_factory; tick() is driven
#   synchronously and the activated futures are waited on deterministically
#   (activate returns the Future; the worker deregisters in finally, so
#   Future.result() also means the registry slot is freed for the next tick).
#   The worker's default credential resolver is overridden via
#   run_group_sync.__kwdefaults__ (the Sched 2 seam) so no secret backend is
#   needed -- but the SCHEDULER still reads REAL seeded credential rows to decide
#   what to submit, which is the code path under test. Unique per-module tenant
#   ids + string keys let the module purge clean a crashed run's leftovers
#   without touching neighbouring PG suites.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> subject.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> the worker
#     each submitted job runs (_run_group_sync_job) + the failure audit sibling.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py -> the
#     per-transaction RLS boundary the two-tenant test proves against real RLS.
#   - File: tests/connectors/runs/test_executor_rls_postgres.py -> the credential
#     seeding + owner-engine read-back style this module mirrors.
# ============================================================================
"""Postgres-tier proof for the scheduled CMS group sync: per-tenant RLS + audit."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.config.settings import (
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    load_app_settings,
)
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
)
from ums_smart_revenue.connectors.keys import YOUTUBE_ANALYTICS_CONNECTOR
from ums_smart_revenue.connectors.runs.executor import (
    ConnectorJobActor,
    ConnectorJobExecutor,
)
from ums_smart_revenue.connectors.runs.group_sync import run_group_sync
from ums_smart_revenue.connectors.runs.scheduler import GroupSyncScheduler
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.db.session import build_session_factory

# Fixed, module-unique ids ("5ced" == "sched"): the purge keys on these, so a
# crashed run's leftovers are reachable and neighbouring PG suites (which use
# uuid4 tenants + other connector keys) are never touched.
TENANT_A = UUID("5ced9a00-0000-0000-0000-0000000000a1")
TENANT_B = UUID("5ced9a00-0000-0000-0000-0000000000b2")
TENANT_C = UUID("5ced9a00-0000-0000-0000-0000000000c3")
USER_A = UUID("5ced9a00-0000-0000-0000-00000000a001")
USER_B = UUID("5ced9a00-0000-0000-0000-00000000b002")
# str: used both as the UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID env value and as a
# ConnectorJobActor.user_id (which is a str field).
SERVICE_ACTOR_ID = "5ced9a00-0000-0000-0000-00005e971ce0"

_ALL_TENANTS = (TENANT_A, TENANT_B, TENANT_C)

OWNER_A = "SchedPgOwnerAAAAAAAAAA"
OWNER_B = "SchedPgOwnerBBBBBBBBBB"
OWNER_C = "SchedPgOwnerCCCCCCCCCC"

CHAN_A1 = "UCSchedGrpSyncA100000001"
CHAN_A2 = "UCSchedGrpSyncA200000002"
CHAN_B1 = "UCSchedGrpSyncB100000001"
CHAN_B2 = "UCSchedGrpSyncB200000002"

GROUP_A = "sched-pg-cms-a"
GROUP_B = "sched-pg-cms-b"

# owner -> [(cms_group_id, title, member_channel_ids), ...].
_Snapshots = dict[str, list[tuple[str, str, tuple[str, ...]]]]

_UPGRADED_URLS: set[str] = set()


class FakeGroupsClient:
    """Canned multi-owner stand-in for YouTubeGroupsClient; never performs I/O.

    Branches on ``account_id`` so ONE client serves several content owners in a
    multi-tenant tick. An UNKNOWN owner returns an empty group list: this suite
    does not own the whole shared container, so a stray ACTIVE tenant enumerated
    by the scheduler must degrade to a harmless no-op sync, not a KeyError (it
    has no youtube-analytics credential in practice, but the tolerance keeps the
    tick deterministic regardless).
    """

    def __init__(self, snapshots: _Snapshots) -> None:
        """Hold a live reference to the (possibly later-mutated) snapshot map."""
        self._snapshots = snapshots

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list for ``account_id`` (empty if unknown)."""
        return [
            CmsGroup(cms_group_id=key, title=title)
            for key, title, _ids in self._snapshots.get(account_id, [])
        ]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group for ``account_id``."""
        for key, _title, channel_ids in self._snapshots.get(account_id, []):
            if key == group_id:
                return CmsGroupMembers(channel_ids=channel_ids, non_channel_count=0)
        raise AssertionError(f"unexpected group_id {group_id} for {account_id}")

    def close(self) -> None:
        """No-op: this double never opens a real HTTP client."""


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without an ini file.

    Mirrors the no-ini pattern in the neighbouring PG suites: configuring
    script_location + sqlalchemy.url directly avoids env.py touching the logging
    tree and silencing other tests' caplog for the rest of the session.
    """
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


def _ensure_upgraded(url: str) -> None:
    """Migrate the shared disposable Postgres database to head once per session."""
    if url in _UPGRADED_URLS:
        return
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED_URLS.add(url)


def _seed_scaffolding(engine: sa.Engine) -> None:
    """Seed the three ACTIVE tenants + the two credential-owner users, once.

    Runs as the schema owner (RLS is ENABLE not FORCE, so the owner login writes
    every table directly). Idempotent via ON CONFLICT so re-running the module
    against a warm container is harmless. Tenants + users are stable scaffolding
    and are NOT purged between tests -- only the domain/audit rows are.
    """
    with engine.begin() as conn:
        for tenant_id, slug in (
            (TENANT_A, "sched-pg-a"),
            (TENANT_B, "sched-pg-b"),
            (TENANT_C, "sched-pg-c"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
                    "VALUES (:id, :slug, :name, 'USD', 'ACTIVE') ON CONFLICT (id) DO NOTHING"
                ),
                {"id": tenant_id, "slug": slug, "name": slug.upper()},
            )
        for user_id, tenant_id in ((USER_A, TENANT_A), (USER_B, TENANT_B)):
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tid, :email, 'Seed') ON CONFLICT (id) DO NOTHING"
                ),
                {"id": user_id, "tid": tenant_id, "email": f"seed-{user_id}@example.com"},
            )


def _purge_test_rows(engine: sa.Engine) -> None:
    """Remove every domain/audit row this module writes, for its three tenants.

    Runs as the owner (RLS-bypassing) connection so it reaches rows written under
    any tenant lane. Keyed on this module's fixed tenant ids -- unique to this
    suite -- so leftovers from a crashed run are cleaned while other PG suites'
    rows stay untouched. channel_group_members first for the FK to channel_groups
    / youtube_channels; audit_logs last so each test's count baseline is zero.
    """
    with engine.begin() as conn:
        for tenant_id in _ALL_TENANTS:
            for table in (
                "channel_group_members",
                "channel_groups",
                "youtube_channels",
                "api_connector_credentials",
                "audit_logs",
            ):
                conn.execute(
                    sa.text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )


def _seed_credential(engine: sa.Engine, *, tenant_id: UUID, owner: str, user_id: UUID) -> None:
    """Insert one ACTIVE youtube-analytics credential with account_id == owner.

    This is what the SCHEDULER enumerates (list_credentials) to decide it should
    submit a sync job for ``owner`` under ``tenant_id``. The secret ref is a
    well-formed external-secret locator; the worker's real resolver is bypassed
    in the happy path, so the secret is never dereferenced.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO api_connector_credentials (id, tenant_id, connector_key, "
                "account_id, encrypted_secret_ref, status, created_by, updated_by) "
                "VALUES (:id, :tid, :key, :acct, :ref, 'active', :by, :by)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "key": YOUTUBE_ANALYTICS_CONNECTOR,
                "acct": owner,
                "ref": "secret-manager://sched-pg/creds",
                "by": user_id,
            },
        )


def _seed_channel(
    engine: sa.Engine, *, tenant_id: UUID, channel_id: str, owner: str, name: str
) -> None:
    """Insert one INSIDE_CMS channel so a CMS member has something to attach to.

    The sync never creates channels (unknown CMS members are surfaced, not
    invented), so every member id in a snapshot must already exist here for the
    mirror to land a membership row.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO youtube_channels (id, tenant_id, youtube_channel_id, "
                "channel_name, cms_status, content_owner_id, revenue_required, "
                "revenue_source_status, active) VALUES (gen_random_uuid(), :tid, :cid, "
                ":name, 'INSIDE_CMS', :owner, FALSE, 'PERFORMANCE_ONLY', TRUE)"
            ),
            {"tid": tenant_id, "cid": channel_id, "name": name, "owner": owner},
        )


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL, migrate it, and seed scaffolding."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    engine = sa.create_engine(url)
    try:
        _seed_scaffolding(engine)
    finally:
        engine.dispose()
    return url


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an RLS-bypassing owner engine, purging this module's rows either side."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_test_rows(engine)
        yield engine
        _purge_test_rows(engine)
    finally:
        engine.dispose()


@pytest.fixture
def service_actor(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set the connector service-actor env so the worker can build its principal.

    Mirrors the connector PG suites: setenv + settings-cache clear (the worker's
    build_connector_service_principal reads through the cached load_app_settings).
    """
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    try:
        yield SERVICE_ACTOR_ID
    finally:
        load_app_settings.cache_clear()


@pytest.fixture
def resolves_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the worker's default credential resolver so no secret backend is needed.

    The worker calls run_group_sync WITHOUT a resolver arg (production resolves
    through the real orchestrator resolver). Its default is bound at def time, so
    overriding the keyword-only default in ``__kwdefaults__`` is the supported
    seam (the Sched 2 unit tests use the same one) and returns an opaque
    credentials object the fake client ignores. The SCHEDULER's own credential
    read (list_credentials against the seeded rows) is a different code path and
    is NOT bypassed -- it stays under test.
    """

    def _resolver(**_kwargs: object) -> object:
        return object()

    assert run_group_sync.__kwdefaults__ is not None
    monkeypatch.setitem(run_group_sync.__kwdefaults__, "resolver", _resolver)


def _build_executor(factory: object, snapshots: _Snapshots) -> ConnectorJobExecutor:
    """Build a real single-worker executor whose group-sync client is the fake.

    The factory receives resolved credentials and ignores them; it re-reads
    ``snapshots`` on every job, so a test mutating that dict between ticks changes
    what the next job sees.
    """
    return ConnectorJobExecutor(
        session_factory=factory,  # type: ignore[arg-type]
        max_workers=1,
        stale_running_hours=6,
        group_sync_client_factory=lambda _credentials: FakeGroupsClient(snapshots),
    )


def _record_activations(executor: ConnectorJobExecutor) -> list[Future]:
    """Wrap ``executor.activate`` so the futures the scheduler creates are captured.

    tick() calls ``self._executor.activate(reservation)`` internally and discards
    the Future; shadowing the bound method with an instance attribute lets the
    test wait on exactly the jobs a tick submitted. The registry also empties on
    completion (the worker deregisters in finally), so Future.result() doubles as
    "the slot is free for the next tick".
    """
    activated: list[Future] = []
    real_activate = executor.activate

    def _recording(reservation: object) -> Future:
        future = real_activate(reservation)  # type: ignore[arg-type]
        activated.append(future)
        return future

    executor.activate = _recording  # type: ignore[assignment]
    return activated


def _tick_and_wait(
    scheduler: GroupSyncScheduler, activated: list[Future], *, timeout: float = 30.0
) -> list[Future]:
    """Drive one tick and block on the jobs IT submitted; return those futures."""
    before = len(activated)
    scheduler.tick()
    submitted = activated[before:]
    for future in submitted:
        future.result(timeout=timeout)
    return submitted


def _stored_group(
    engine: sa.Engine, *, tenant_id: UUID, cms_group_id: str
) -> tuple[str, str, set[str]] | None:
    """Return (content_owner_id, name, member_youtube_ids) for one group, or None.

    Read through the owner engine (RLS-bypassing) and filtered by tenant_id, so a
    row leaked under the wrong tenant would surface here. Values are extracted
    inside the session to avoid touching detached/expired ORM attributes.
    """
    with Session(engine) as read:
        group = read.scalars(
            select(ChannelGroupORM).where(
                ChannelGroupORM.tenant_id == tenant_id,
                ChannelGroupORM.cms_group_id == cms_group_id,
            )
        ).one_or_none()
        if group is None:
            return None
        members = set(
            read.scalars(
                select(YouTubeChannelORM.youtube_channel_id)
                .join(
                    ChannelGroupMemberORM,
                    ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id,
                )
                .where(ChannelGroupMemberORM.group_id == group.id)
            ).all()
        )
        return (group.content_owner_id, group.name, members)


def _audit_events(engine: sa.Engine, *, tenant_id: UUID) -> list[tuple[str, dict]]:
    """Return (event_type, details) for every audit row of one tenant (owner read)."""
    with Session(engine) as read:
        rows = read.scalars(select(AuditLogORM).where(AuditLogORM.tenant_id == tenant_id)).all()
        return [(row.event_type, dict(row.details or {})) for row in rows]


def _audit_count(engine: sa.Engine, *, tenant_id: UUID) -> int:
    """Count one tenant's audit_logs rows through the RLS-bypassing owner engine."""
    with Session(engine) as read:
        return read.scalar(
            select(sa.func.count())
            .select_from(AuditLogORM)
            .where(AuditLogORM.tenant_id == tenant_id)
        )


def _scheduler(factory: object, executor: ConnectorJobExecutor) -> GroupSyncScheduler:
    """Build a real scheduler over the PG session factory (never start()ed here)."""
    return GroupSyncScheduler(
        session_factory=factory,  # type: ignore[arg-type]
        executor=executor,
        interval_seconds=3600,
        service_actor_id=SERVICE_ACTOR_ID,
    )


def test_scheduled_tick_converges_one_tenant_end_to_end(
    pg_url: str, owner_engine: sa.Engine, service_actor: str, resolves_ok: None
) -> None:
    """One ACTIVE tenant + one active credential -> tick -> full mirror on real PG.

    The end-to-end convergence proof: a single tick submits exactly one job whose
    worker lands the group row (owner-stamped), both membership rows, the per-
    group GROUP_UPDATED audit row (details.source == "cms_group_sync"), and the
    run-level GROUPS_SYNCED summary -- all under tenant A's tenant_id.
    """
    _seed_credential(owner_engine, tenant_id=TENANT_A, owner=OWNER_A, user_id=USER_A)
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A1, owner=OWNER_A, name="A One")
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A2, owner=OWNER_A, name="A Two")
    snapshots: _Snapshots = {OWNER_A: [(GROUP_A, "Alpha Sector", (CHAN_A1, CHAN_A2))]}

    factory = build_session_factory(pg_url)
    executor = _build_executor(factory, snapshots)
    activated = _record_activations(executor)
    scheduler = _scheduler(factory, executor)
    try:
        submitted = _tick_and_wait(scheduler, activated)
    finally:
        scheduler.close()
        executor.close()

    assert len(submitted) == 1, "exactly tenant A's youtube-analytics credential should submit"

    stored = _stored_group(owner_engine, tenant_id=TENANT_A, cms_group_id=GROUP_A)
    assert stored is not None, "tenant A's mirrored group must exist on the real engine"
    content_owner_id, name, members = stored
    assert content_owner_id == OWNER_A
    assert name == "Alpha Sector"
    assert members == {CHAN_A1, CHAN_A2}

    events = _audit_events(owner_engine, tenant_id=TENANT_A)
    group_updated = [d for etype, d in events if etype == AuditEventType.GROUP_UPDATED.value]
    assert group_updated, "a per-group GROUP_UPDATED audit row must land"
    assert all(d.get("source") == "cms_group_sync" for d in group_updated)
    summaries = [d for etype, d in events if etype == AuditEventType.GROUPS_SYNCED.value]
    assert len(summaries) == 1, "exactly one run-level GROUPS_SYNCED summary row"
    assert summaries[0]["content_owner_id"] == OWNER_A
    assert summaries[0]["counts"]["CREATE"] == 1


def test_scheduled_tick_isolates_two_tenants_under_rls(
    pg_url: str, owner_engine: sa.Engine, service_actor: str, resolves_ok: None
) -> None:
    """TWO ACTIVE tenants, ONE tick: each converges under ITS tenant_id (the Sched-3 proof).

    Both tenants are enumerated in the SAME shared scheduler session, so the per-
    tenant rollback is exercised for real. If it were missing, the second tenant's
    list_credentials would run inside the first tenant's still-open, RLS-pinned
    transaction and its credential would be invisible -- so only one job would
    submit and that tenant's group would be absent below. Cross-absence is checked
    via owner-engine (platform-lane) reads filtered by tenant_id.
    """
    _seed_credential(owner_engine, tenant_id=TENANT_A, owner=OWNER_A, user_id=USER_A)
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A1, owner=OWNER_A, name="A One")
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A2, owner=OWNER_A, name="A Two")
    _seed_credential(owner_engine, tenant_id=TENANT_B, owner=OWNER_B, user_id=USER_B)
    _seed_channel(owner_engine, tenant_id=TENANT_B, channel_id=CHAN_B1, owner=OWNER_B, name="B One")
    _seed_channel(owner_engine, tenant_id=TENANT_B, channel_id=CHAN_B2, owner=OWNER_B, name="B Two")
    snapshots: _Snapshots = {
        OWNER_A: [(GROUP_A, "Alpha Sector", (CHAN_A1, CHAN_A2))],
        OWNER_B: [(GROUP_B, "Bravo Sector", (CHAN_B1, CHAN_B2))],
    }

    factory = build_session_factory(pg_url)
    executor = _build_executor(factory, snapshots)
    activated = _record_activations(executor)
    scheduler = _scheduler(factory, executor)
    try:
        submitted = _tick_and_wait(scheduler, activated)
    finally:
        scheduler.close()
        executor.close()

    assert len(submitted) == 2, "both tenants' credentials must yield a job in one tick"

    a_group = _stored_group(owner_engine, tenant_id=TENANT_A, cms_group_id=GROUP_A)
    b_group = _stored_group(owner_engine, tenant_id=TENANT_B, cms_group_id=GROUP_B)
    assert a_group is not None, "tenant A's group must exist under tenant A"
    assert b_group is not None, "tenant B's group must exist under tenant B (the Sched-3 proof)"
    assert a_group[0] == OWNER_A
    assert b_group[0] == OWNER_B
    assert a_group[2] == {CHAN_A1, CHAN_A2}
    assert b_group[2] == {CHAN_B1, CHAN_B2}

    # Cross-absence: neither tenant carries the other's group.
    assert _stored_group(owner_engine, tenant_id=TENANT_A, cms_group_id=GROUP_B) is None
    assert _stored_group(owner_engine, tenant_id=TENANT_B, cms_group_id=GROUP_A) is None


def test_scheduled_reticks_are_idempotent_then_a_change_writes(
    pg_url: str, owner_engine: sa.Engine, service_actor: str, resolves_ok: None
) -> None:
    """A converged re-tick writes ZERO new audit rows; a renamed snapshot then writes rows.

    The twin (a third tick with a renamed group) is the anti-vacuity guard: it
    proves the zero-assertion is not trivially true because the worker never
    writes anything.
    """
    _seed_credential(owner_engine, tenant_id=TENANT_A, owner=OWNER_A, user_id=USER_A)
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A1, owner=OWNER_A, name="A One")
    _seed_channel(owner_engine, tenant_id=TENANT_A, channel_id=CHAN_A2, owner=OWNER_A, name="A Two")
    snapshots: _Snapshots = {OWNER_A: [(GROUP_A, "Alpha Sector", (CHAN_A1, CHAN_A2))]}

    factory = build_session_factory(pg_url)
    executor = _build_executor(factory, snapshots)
    activated = _record_activations(executor)
    scheduler = _scheduler(factory, executor)
    try:
        # Tick 1: create.
        _tick_and_wait(scheduler, activated)
        after_create = _audit_count(owner_engine, tenant_id=TENANT_A)
        assert after_create > 0, "the first tick must write audit rows"

        # Tick 2: identical snapshot -> everything UNCHANGED -> ZERO new rows.
        _tick_and_wait(scheduler, activated)
        assert _audit_count(owner_engine, tenant_id=TENANT_A) == after_create
        unchanged = _stored_group(owner_engine, tenant_id=TENANT_A, cms_group_id=GROUP_A)
        assert unchanged is not None and unchanged[1] == "Alpha Sector"

        # Twin: a renamed group DOES add rows, so the zero above is not vacuous.
        snapshots[OWNER_A] = [(GROUP_A, "Alpha Sector HD", (CHAN_A1, CHAN_A2))]
        _tick_and_wait(scheduler, activated)
        assert _audit_count(owner_engine, tenant_id=TENANT_A) > after_create
        renamed = _stored_group(owner_engine, tenant_id=TENANT_A, cms_group_id=GROUP_A)
        assert renamed is not None and renamed[1] == "Alpha Sector HD"
    finally:
        scheduler.close()
        executor.close()


def test_missing_credential_job_audits_failure_cross_lane(
    pg_url: str, owner_engine: sa.Engine, service_actor: str
) -> None:
    """A job for an owner with no credential lands one cross-lane group_sync_job_failed row.

    NOTE: this exercises the WORKER's failure path, not the scheduler. tick()
    only submits owners it FINDS an active credential for, so a tenant with none
    submits nothing -- which is correct. To reach the worker's missing-credential
    path we submit directly (submit_group_sync_if_absent + activate), exactly what
    the scheduler's activate call does, minus the enumeration that skips this
    owner. No resolves_ok fixture here: the REAL resolver must run and raise
    CredentialNotFoundError. The resulting audit uses the fresh-session platform_
    lane + placeholder-tenant TENANT_CTX bridge, proven cross-lane on real RLS.
    """
    factory = build_session_factory(pg_url)
    fake_actor = ConnectorJobActor(user_id=SERVICE_ACTOR_ID, email="ops@example.com")
    executor = ConnectorJobExecutor(
        session_factory=factory,
        max_workers=1,
        stale_running_hours=6,
        group_sync_client_factory=lambda _credentials: FakeGroupsClient({}),
    )
    try:
        reservation = executor.submit_group_sync_if_absent(
            tenant_id=TENANT_C, content_owner_id=OWNER_C, actor_identity=fake_actor
        )
        assert reservation is not None
        executor.activate(reservation).result(timeout=30)
    finally:
        executor.close()

    events = _audit_events(owner_engine, tenant_id=TENANT_C)
    failures = [
        d
        for etype, d in events
        if etype == AuditEventType.CONNECTOR_JOB_RUN.value
        and d.get("action") == "group_sync_job_failed"
    ]
    assert len(failures) == 1, failures
    assert failures[0]["error_class"] == "CredentialNotFoundError"
    assert failures[0]["content_owner_id"] == OWNER_C
