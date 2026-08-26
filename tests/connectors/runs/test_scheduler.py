# ============================================================================
# Purpose: Pin GroupSyncScheduler.tick() — exactly which (tenant, content
#   owner) pairs get submitted from a mixed fleet (ACTIVE/SUSPENDED tenants,
#   active/inactive credentials, other-connector credentials), pagination
#   across list_credentials pages, per-tenant fault isolation, in-flight
#   dedup skip (no activate() call), the tick-level catch-all
#   (_tick_safely never lets a poisoned tick escape), the start/close
#   thread lifecycle (prompt join, double-close and never-started-close both
#   harmless), the stop-flag early-abort guards, the surviving-thread close
#   warning, and the abandoned-scheduler GC backstop.
# Database/ORM: a real SQLite session factory (TenantBase + SecurityBase
#   metadata only -- no org/report tables needed, this suite never touches
#   channel groups or connector runs) seeded directly via ORM inserts.
# Standards: the fake executor is a hand-written recorder exposing
#   submit_group_sync_if_absent/activate with the SAME keyword-only
#   signatures ConnectorJobExecutor exposes -- not a MagicMock, so a
#   signature typo in the scheduler's call sites raises TypeError here
#   instead of being silently swallowed. tick() is driven directly with no
#   clock and no sleep; the ONE threaded test (start/close) is deterministic
#   via Event.wait() being woken by close(), never a real sleep.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> subject.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> the
#     submit_group_sync_if_absent / activate signatures this suite's fake
#     mirrors.
#   - File: tests/connectors/runs/test_group_sync_jobs.py -> the SQLite
#     session-factory fixture pattern this suite's _factory mirrors.
# ============================================================================
"""Unit tests for the in-process CMS group-sync scheduler."""

from __future__ import annotations

import gc
import hashlib
import logging
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.audit import _SERVICE_ACCOUNT_EMAIL
from ums_smart_revenue.connectors.keys import YOUTUBE_ANALYTICS_CONNECTOR
from ums_smart_revenue.connectors.runs import scheduler as scheduler_module
from ums_smart_revenue.connectors.runs.executor import ConnectorJobActor
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, SecurityBase
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.models import TenantStatus

SERVICE_ACTOR_ID = str(uuid4())
EXPECTED_ACTOR = ConnectorJobActor(user_id=SERVICE_ACTOR_ID, email=_SERVICE_ACCOUNT_EMAIL)

TENANT_A = uuid4()
TENANT_B = uuid4()
TENANT_SUSPENDED = uuid4()

OWNER_A_ACTIVE = "OwnerAActiveAAAAAAAAAA"
OWNER_A_SECOND_ACTIVE = "OwnerASecondActiveAAAA"
OWNER_A_INACTIVE = "OwnerAInactiveXXXXXXXX"
OWNER_A_OTHER_CONNECTOR = "OwnerAOtherConnXXXXXXX"
OWNER_B_ACTIVE = "OwnerBActiveBBBBBBBBBB"
OWNER_SUSPENDED = "OwnerSuspendedXXXXXXXX"

OTHER_CONNECTOR_KEY = "youtube_reporting"


@dataclass(frozen=True)
class _RecordedSubmission:
    """One recorded submit_group_sync_if_absent call, reused as the fake's reservation."""

    tenant_id: UUID
    content_owner_id: str
    actor_identity: ConnectorJobActor


class _RecordingExecutor:
    """Hand-written ConnectorJobExecutor stand-in — NOT a MagicMock.

    Exposes exactly the two keyword-only methods the scheduler calls, with the
    SAME parameter names ConnectorJobExecutor uses, so a typo or signature
    drift in the scheduler's call sites raises ``TypeError`` here instead of a
    MagicMock silently accepting whatever kwargs it is handed.
    """

    def __init__(self, *, in_flight: frozenset[tuple[UUID, str]] = frozenset()) -> None:
        """Record every attempted submission plus which ones were actually activated."""
        self._in_flight = in_flight
        self.attempted: list[tuple[UUID, str]] = []
        self.submissions: list[_RecordedSubmission] = []
        self.activated: list[_RecordedSubmission] = []

    def submit_group_sync_if_absent(
        self,
        *,
        tenant_id: UUID,
        content_owner_id: str,
        actor_identity: ConnectorJobActor,
    ) -> _RecordedSubmission | None:
        """Mirror ConnectorJobExecutor.submit_group_sync_if_absent's reserve-or-None contract."""
        self.attempted.append((tenant_id, content_owner_id))
        if (tenant_id, content_owner_id) in self._in_flight:
            return None
        submission = _RecordedSubmission(
            tenant_id=tenant_id,
            content_owner_id=content_owner_id,
            actor_identity=actor_identity,
        )
        self.submissions.append(submission)
        return submission

    def activate(self, reservation: _RecordedSubmission) -> _RecordedSubmission:
        """Mirror ConnectorJobExecutor.activate's one-positional-arg signature."""
        self.activated.append(reservation)
        return reservation


def _factory(tmp_path: Path, *, db_name: str = "scheduler.db") -> sessionmaker:
    """Build a SQLite session factory with only the tables this suite needs.

    No org/report metadata: this suite never seeds a channel, a group, or a
    connector run -- only tenants and connector credentials.
    """
    url = f"sqlite+pysqlite:///{(tmp_path / db_name).as_posix()}"
    engine = create_engine(url)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_tenant(session: Session, *, tenant_id: UUID, status: TenantStatus, slug: str) -> None:
    """Insert one tenant row with the given lifecycle status."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session.add(
        TenantORM(
            id=tenant_id,
            slug=slug,
            display_name=slug,
            primary_currency="USD",
            status=status,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_credential(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    status: str = "active",
) -> None:
    """Insert one connector credential row directly (bypassing the repository)."""
    session.add(
        ApiConnectorCredentialORM(
            id=uuid4(),
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            encrypted_secret_ref="secret-manager://scheduler-test",
            status=status,
        )
    )


def _seeded_factory(tmp_path: Path) -> sessionmaker:
    """Two ACTIVE tenants + one SUSPENDED, with mixed credentials on tenant A.

    Tenant A: an active youtube-analytics credential (should submit), an
    inactive (``disabled``) youtube-analytics credential (should NOT submit),
    and an active credential under a DIFFERENT connector (should NOT submit).
    Tenant B: one active youtube-analytics credential (should submit).
    The suspended tenant: an active youtube-analytics credential that must
    NEVER be reached because connector_tenant_context refuses a non-ACTIVE
    tenant before any credential read happens.
    """
    factory = _factory(tmp_path)
    with factory() as session:
        _seed_tenant(session, tenant_id=TENANT_A, status=TenantStatus.ACTIVE, slug="tenant-a")
        _seed_tenant(session, tenant_id=TENANT_B, status=TenantStatus.ACTIVE, slug="tenant-b")
        _seed_tenant(
            session,
            tenant_id=TENANT_SUSPENDED,
            status=TenantStatus.SUSPENDED,
            slug="tenant-suspended",
        )
        _seed_credential(
            session,
            tenant_id=TENANT_A,
            connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
            account_id=OWNER_A_ACTIVE,
            status="active",
        )
        _seed_credential(
            session,
            tenant_id=TENANT_A,
            connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
            account_id=OWNER_A_INACTIVE,
            status="disabled",
        )
        _seed_credential(
            session,
            tenant_id=TENANT_A,
            connector_key=OTHER_CONNECTOR_KEY,
            account_id=OWNER_A_OTHER_CONNECTOR,
            status="active",
        )
        _seed_credential(
            session,
            tenant_id=TENANT_B,
            connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
            account_id=OWNER_B_ACTIVE,
            status="active",
        )
        _seed_credential(
            session,
            tenant_id=TENANT_SUSPENDED,
            connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
            account_id=OWNER_SUSPENDED,
            status="active",
        )
        session.commit()
    return factory


def _scheduler(
    factory: sessionmaker,
    fake: _RecordingExecutor,
    *,
    interval_seconds: float = 3600.0,
) -> scheduler_module.GroupSyncScheduler:
    """Build a GroupSyncScheduler against a fake executor (centralizes the type: ignore)."""
    return scheduler_module.GroupSyncScheduler(
        session_factory=factory,
        executor=fake,  # type: ignore[arg-type]
        interval_seconds=interval_seconds,
        service_actor_id=SERVICE_ACTOR_ID,
    )


# ---------------------------------------------------------------------------
# Exactly the expected set submitted; SUSPENDED / inactive / other-connector
# owners are never reached.
# ---------------------------------------------------------------------------


def test_tick_submits_exactly_active_youtube_analytics_credentials(tmp_path: Path) -> None:
    """Only tenant A's and tenant B's ACTIVE youtube-analytics owners are submitted+activated."""
    factory = _seeded_factory(tmp_path)
    fake = _RecordingExecutor()
    scheduler = _scheduler(factory, fake)

    scheduler.tick()

    expected = {(TENANT_A, OWNER_A_ACTIVE), (TENANT_B, OWNER_B_ACTIVE)}
    submitted = {(s.tenant_id, s.content_owner_id) for s in fake.submissions}
    activated = {(s.tenant_id, s.content_owner_id) for s in fake.activated}
    assert submitted == expected
    assert activated == expected
    for submission in fake.submissions:
        assert submission.actor_identity == EXPECTED_ACTOR
    # Never even attempted: the suspended tenant's credential read never
    # happens because connector_tenant_context refuses before list_credentials
    # is called for that tenant.
    assert (TENANT_SUSPENDED, OWNER_SUSPENDED) not in fake.attempted
    assert (TENANT_A, OWNER_A_INACTIVE) not in fake.attempted
    assert (TENANT_A, OWNER_A_OTHER_CONNECTOR) not in fake.attempted


# ---------------------------------------------------------------------------
# Pagination: list_credentials pages are fully traversed.
# ---------------------------------------------------------------------------


def test_tick_paginates_through_all_credential_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page size of 1 forces multi-page traversal; every seeded owner still arrives."""
    factory = _factory(tmp_path)
    owners = [f"PagedOwner{i:02d}AAAAAAAAAAA" for i in range(4)]
    with factory() as session:
        _seed_tenant(session, tenant_id=TENANT_A, status=TenantStatus.ACTIVE, slug="tenant-a")
        for owner in owners:
            _seed_credential(
                session,
                tenant_id=TENANT_A,
                connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
                account_id=owner,
                status="active",
            )
        session.commit()
    # list_credentials' own floor (limit >= 1) is the smallest legal page size,
    # guaranteeing every one of the 4 seeded rows lands on its own page.
    monkeypatch.setattr(scheduler_module, "_CREDENTIAL_LIST_PAGE_SIZE", 1)
    fake = _RecordingExecutor()
    scheduler = _scheduler(factory, fake)

    scheduler.tick()

    submitted_owners = {s.content_owner_id for s in fake.submissions}
    assert submitted_owners == set(owners)


# ---------------------------------------------------------------------------
# Fault isolation: one tenant's context raising does not starve the rest.
# ---------------------------------------------------------------------------


def test_tick_isolates_one_tenant_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant A's connector_tenant_context raises; tenant B is still submitted."""
    factory = _seeded_factory(tmp_path)
    real_ctx = scheduler_module.connector_tenant_context

    @contextmanager
    def _flaky_ctx(tenant_id: UUID, *, session: Session | None = None) -> Iterator[None]:
        if tenant_id == TENANT_A:
            raise RuntimeError("boom for tenant A")
        with real_ctx(tenant_id, session=session):
            yield

    monkeypatch.setattr(scheduler_module, "connector_tenant_context", _flaky_ctx)
    fake = _RecordingExecutor()
    scheduler = _scheduler(factory, fake)

    scheduler.tick()  # must not raise -- per-tenant isolation, not the catch-all

    submitted = {(s.tenant_id, s.content_owner_id) for s in fake.submissions}
    assert submitted == {(TENANT_B, OWNER_B_ACTIVE)}


# ---------------------------------------------------------------------------
# In-flight dedup: a None reservation skips activation, others unaffected.
# ---------------------------------------------------------------------------


def test_tick_skips_activation_for_in_flight_owner(tmp_path: Path) -> None:
    """An owner already in flight is attempted but never activated; the rest still are."""
    factory = _seeded_factory(tmp_path)
    fake = _RecordingExecutor(in_flight=frozenset({(TENANT_A, OWNER_A_ACTIVE)}))
    scheduler = _scheduler(factory, fake)

    scheduler.tick()

    assert (TENANT_A, OWNER_A_ACTIVE) in fake.attempted
    activated = {(s.tenant_id, s.content_owner_id) for s in fake.activated}
    assert (TENANT_A, OWNER_A_ACTIVE) not in activated
    assert (TENANT_B, OWNER_B_ACTIVE) in activated


def test_in_flight_debug_log_fingerprints_the_owner(tmp_path: Path, caplog) -> None:
    """DEBUG output never carries the raw guarded CMS content-owner id; README
    presents UMS_LOG_LEVEL=DEBUG as operator-safe (PR #210 review round 5)."""
    factory = _seeded_factory(tmp_path)
    fake = _RecordingExecutor(in_flight=frozenset({(TENANT_A, OWNER_A_ACTIVE)}))
    scheduler = _scheduler(factory, fake)

    with caplog.at_level(
        logging.DEBUG, logger="ums_smart_revenue.connectors.runs.scheduler"
    ):
        scheduler.tick()

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert OWNER_A_ACTIVE not in log_text
    fingerprint = hashlib.sha256(OWNER_A_ACTIVE.encode("utf-8")).hexdigest()[:12]
    assert f"owner_fp={fingerprint}" in log_text


# ---------------------------------------------------------------------------
# Catch-all: a tick where enumeration itself raises never escapes _tick_safely.
# ---------------------------------------------------------------------------


def test_tick_safely_swallows_enumeration_failure() -> None:
    """tick() itself raises on a broken session_factory; _tick_safely swallows it."""

    def _boom_factory() -> Session:
        raise RuntimeError("db unavailable")

    scheduler = scheduler_module.GroupSyncScheduler(
        session_factory=_boom_factory,  # type: ignore[arg-type]
        executor=_RecordingExecutor(),  # type: ignore[arg-type]
        interval_seconds=3600.0,
        service_actor_id=SERVICE_ACTOR_ID,
    )

    # Anti-vacuity: prove tick() really does propagate before trusting that
    # _tick_safely is the thing swallowing it.
    with pytest.raises(RuntimeError, match="db unavailable"):
        scheduler.tick()

    scheduler._tick_safely()  # must not raise


# ---------------------------------------------------------------------------
# start/close thread lifecycle.
# ---------------------------------------------------------------------------


def test_start_close_lifecycle_joins_promptly(tmp_path: Path) -> None:
    """close() wakes the Event.wait() immediately and joins well within its timeout."""
    factory = _factory(tmp_path)
    fake = _RecordingExecutor()
    # A long interval proves close() does not wait for the next scheduled
    # tick -- Event.set() wakes Event.wait(3600) immediately.
    scheduler = _scheduler(factory, fake, interval_seconds=3600.0)

    scheduler.start()
    assert scheduler._thread is not None
    assert scheduler._thread.is_alive()

    scheduler.close()
    assert scheduler._thread.is_alive() is False

    scheduler.close()  # double-close is harmless


def test_close_without_start_is_harmless(tmp_path: Path) -> None:
    """A scheduler that was never started closes cleanly (no thread to join)."""
    factory = _factory(tmp_path)
    scheduler = _scheduler(factory, _RecordingExecutor())

    scheduler.close()  # must not raise


# ---------------------------------------------------------------------------
# close() vs in-flight tick: the stop flag blocks NEW submissions at every
# loop level, and a thread outliving the bounded join is warned, not raced.
# ---------------------------------------------------------------------------


def test_tick_aborts_new_submissions_once_close_begins(tmp_path: Path) -> None:
    """Stop set mid-tick: the current submission finishes, NO new ones start.

    Qodo PR-171 finding ("close returns while thread runs"): close() could
    return while a tick kept submitting into a shutting-down executor. The
    fix: tick() checks the stop flag per tenant and _submit_for_tenant checks
    it per page and per credential, so a closing scheduler halts new
    submissions right after the current one. Two ACTIVE tenants, with TWO
    active credentials on tenant A, prove both levels: tenant A's SECOND
    credential is never attempted (per-entry guard) and tenant B is never
    reached (per-tenant guard).
    """
    factory = _factory(tmp_path)
    with factory() as session:
        _seed_tenant(session, tenant_id=TENANT_A, status=TenantStatus.ACTIVE, slug="tenant-a")
        _seed_tenant(session, tenant_id=TENANT_B, status=TenantStatus.ACTIVE, slug="tenant-b")
        for owner in (OWNER_A_ACTIVE, OWNER_A_SECOND_ACTIVE):
            _seed_credential(
                session,
                tenant_id=TENANT_A,
                connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
                account_id=owner,
                status="active",
            )
        _seed_credential(
            session,
            tenant_id=TENANT_B,
            connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
            account_id=OWNER_B_ACTIVE,
            status="active",
        )
        session.commit()
    fake = _RecordingExecutor()
    scheduler = _scheduler(factory, fake)

    real_activate = fake.activate

    def _closing_activate(reservation: _RecordedSubmission) -> _RecordedSubmission:
        """First activation simulates close() landing mid-tick."""
        scheduler._stop.set()
        return real_activate(reservation)

    fake.activate = _closing_activate  # type: ignore[method-assign]

    scheduler.tick()

    # Exactly ONE attempt+activation: whichever tenant-A credential the page
    # yielded first. The stop flag blocked its sibling and tenant B alike.
    assert len(fake.attempted) == 1
    only = fake.attempted[0]
    assert only[0] == TENANT_A
    assert only[1] in {OWNER_A_ACTIVE, OWNER_A_SECOND_ACTIVE}
    assert [(s.tenant_id, s.content_owner_id) for s in fake.activated] == [only]


def test_close_warns_when_thread_outlives_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tick thread still alive after the bounded join is warned, not silently raced.

    Qodo PR-171 finding: close() used to return without verifying the thread
    exited, letting the app lifespan close the executor while a tick was
    still running. close() now checks is_alive() after the join and logs a
    warning (the stop flag still blocks new submissions from that thread).
    """
    factory = _factory(tmp_path)
    scheduler = _scheduler(factory, _RecordingExecutor())
    gate = threading.Event()
    zombie = threading.Thread(target=gate.wait, name="ums-test-zombie", daemon=True)
    zombie.start()
    scheduler._thread = zombie  # a start()ed thread stuck mid-tick
    monkeypatch.setattr(scheduler_module, "_CLOSE_JOIN_TIMEOUT_SECONDS", 0.01)

    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        scheduler.close()

    gate.set()
    zombie.join(timeout=1)
    assert "still running" in caplog.text
    assert not zombie.is_alive()


def test_abandoned_running_scheduler_is_collected_and_stopped(tmp_path: Path) -> None:
    """Dropping the last strong reference lets GC stop a RUNNING scheduler.

    Qodo PR-171 finding ("finalizer ineffective"): pre-fix, the thread's
    ``target=self._run_loop`` bound method strongly retained the scheduler,
    so an abandoned running scheduler was never collected and the
    ``weakref.finalize`` GC backstop could never fire. Post-fix, the loop
    closure holds only a weakref + the stop event: GC collects the abandoned
    scheduler, the finalizer sets the stop event, and the detached loop
    exits on its next wake.
    """
    factory = _factory(tmp_path)

    def _spawn() -> tuple[threading.Thread, weakref.ref[scheduler_module.GroupSyncScheduler]]:
        """Start a scheduler; its last strong reference dies when this frame exits."""
        scheduler = _scheduler(factory, _RecordingExecutor(), interval_seconds=3600.0)
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
        return scheduler._thread, weakref.ref(scheduler)

    thread, scheduler_ref = _spawn()
    assert thread.is_alive()

    gc.collect()

    # The live thread must NOT keep the scheduler alive: it is collected,
    # the finalizer sets the stop event, and the loop exits promptly.
    assert scheduler_ref() is None
    thread.join(timeout=5)
    assert not thread.is_alive()
