# ============================================================================
# Purpose: Pin the CMS group-sync job kind on ConnectorJobExecutor — registry
#   non-collision with report pulls, dedup, the worker's happy path (group +
#   membership + per-group GROUP_UPDATED + one run-level GROUPS_SYNCED summary,
#   one transaction), the UNCHANGED-only "zero audit rows" rule with its
#   anti-vacuity twin, the failure taxonomy (one group_sync_job_failed row per
#   typed family, concrete error_class), and the queued-at-shutdown behavior.
# Database/ORM: a real SQLite session factory (like test_executor.py's) with a
#   seeded ACTIVE tenant + INSIDE_CMS channel; the worker builds the SAME SQL
#   stores the api dependencies do and writes real rows + AuditLogORM.
# Standards: the worker calls run_group_sync WITHOUT a resolver (production uses
#   the real one, default-bound at def time), so tests that must resolve
#   override run_group_sync.__kwdefaults__["resolver"] — the supported seam,
#   mirroring the Part-1 unit tests' MagicMock-credentials resolver. Failure
#   seams are driven by the REAL run_group_sync (fake client raising, seeded
#   foreign-owner group, empty credential table, suspended tenant, unset actor).
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> subject.
#   - File: backend/ums_smart_revenue/connectors/runs/group_sync.py -> the core.
#   - File: tests/connectors/runs/test_executor.py -> the pull-job precedent
#     this suite's session factory and audit assertions mirror.
# ============================================================================
"""Unit tests for the CMS group-sync job kind on ConnectorJobExecutor."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import record_audit_event as _real_record
from ums_smart_revenue.config.settings import (
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    load_app_settings,
)
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
)
from ums_smart_revenue.connectors.runs.executor import (
    GROUP_SYNC_JOB_CONNECTOR_SLUG,
    GROUP_SYNC_JOB_MONTH,
    ConnectorJobActor,
    ConnectorJobExecutor,
)
from ums_smart_revenue.connectors.runs.group_sync import run_group_sync
from ums_smart_revenue.connectors.runs.orchestrator import ConnectorRunOutcome
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.models import TenantStatus

TENANT = UUID(UMS_TENANT_ID)
ACTOR = ConnectorJobActor(user_id=str(uuid4()), email="ops@example.com")
SERVICE_ACTOR_ID = "11111111-2222-3333-4444-555555555555"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
OTHER_OWNER = "OtherOwnerBBBBBBBBBBBB"
CHANNEL_ONE = "UCB6sc84dcg6VQGB_d89sx2g"

_Snapshot = list[tuple[str, str, tuple[str, ...], int]]


class _FakeGroupsClient:
    """Canned stand-in for YouTubeGroupsClient; never performs any I/O."""

    def __init__(self, snapshot: _Snapshot, *, groups_error: Exception | None = None) -> None:
        """Hold the canned snapshot plus an optional fetch failure to raise."""
        self._snapshot = snapshot
        self._groups_error = groups_error
        self.closed = False

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list, or raise the configured fetch failure."""
        if self._groups_error is not None:
            raise self._groups_error
        return [CmsGroup(cms_group_id=key, title=title) for key, title, _ids, _n in self._snapshot]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group."""
        for key, _title, channel_ids, non_channel in self._snapshot:
            if key == group_id:
                return CmsGroupMembers(channel_ids=channel_ids, non_channel_count=non_channel)
        raise AssertionError(f"unexpected group_id: {group_id}")

    def close(self) -> None:
        """Record that the core closed the client."""
        self.closed = True


def _factory(
    tmp_path: Path,
    *,
    seed_channel: bool = True,
    tenant_status: TenantStatus = TenantStatus.ACTIVE,
) -> sessionmaker:
    """Build a SQLite session factory with an ACTIVE tenant (+ optional channel).

    Mirrors tests/connectors/runs/test_executor.py::_factory: the worker enters
    connector_tenant_context(session=...) and needs the tenant row present with
    the ACTIVE-only gate satisfiable; a seeded INSIDE_CMS channel gives the CMS
    snapshot a member to attach (the sync never invents channels).
    """
    url = f"sqlite+pysqlite:///{(tmp_path / 'gsjobs.db').as_posix()}"
    engine = create_engine(url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            TenantORM(
                id=TENANT,
                slug="ums-test",
                display_name="UMS Test",
                primary_currency="USD",
                status=tenant_status,
                onboarding_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(UserORM(id=UUID(ACTOR.user_id), email=ACTOR.email, display_name="Ops"))
        if seed_channel:
            session.add(
                YouTubeChannelORM(
                    id=uuid4(),
                    tenant_id=TENANT,
                    youtube_channel_id=CHANNEL_ONE,
                    channel_name="Alpha News",
                    cms_status="INSIDE_CMS",
                    content_owner_id=CONTENT_OWNER,
                    revenue_required=False,
                    revenue_source_status="PERFORMANCE_ONLY",
                    active=True,
                )
            )
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _executor(
    factory: sessionmaker,
    *,
    client_factory: Callable[[object], _FakeGroupsClient] | None = None,
) -> ConnectorJobExecutor:
    """Build the executor, optionally injecting a fake groups client factory."""
    kwargs: dict[str, object] = {
        "session_factory": factory,
        "max_workers": 1,
        "stale_running_hours": 6,
    }
    if client_factory is not None:
        kwargs["group_sync_client_factory"] = client_factory
    return ConnectorJobExecutor(**kwargs)  # type: ignore[arg-type]


def _audit_rows(factory: sessionmaker) -> list[AuditLogORM]:
    """Return every audit row on the real engine."""
    with factory() as session:
        return list(session.scalars(select(AuditLogORM)).all())


@pytest.fixture
def service_actor(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the connector service-actor env so the worker builds a service principal."""
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    return SERVICE_ACTOR_ID


@pytest.fixture
def resolves_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the worker's default credential resolver succeed without a secret backend.

    The worker calls run_group_sync WITHOUT a resolver arg by design (production
    resolves through the real orchestrator resolver). Its default is bound at def
    time, so patching the module attribute would not reach the worker; overriding
    the keyword-only default in ``__kwdefaults__`` is the supported seam and
    returns an opaque credentials object the fake client ignores (the Part-1 unit
    tests use the same MagicMock-credentials stand-in).
    """

    def _resolver(**_kwargs: object) -> object:
        return object()

    assert run_group_sync.__kwdefaults__ is not None
    monkeypatch.setitem(run_group_sync.__kwdefaults__, "resolver", _resolver)


# ---------------------------------------------------------------------------
# Registry: non-collision with pulls + dedup
# ---------------------------------------------------------------------------


def test_pull_and_sync_jobs_coexist_in_registry(tmp_path: Path) -> None:
    """A report pull and a group sync for the same tenant+account hold slots at once.

    The reserved connector-key sentinel puts the sync key in a different
    namespace from any real pull, so both reservations live in the registry
    simultaneously without either deduping the other.
    """
    factory = _factory(tmp_path)
    executor = _executor(factory)
    try:
        pull = executor.submit_if_absent(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id=CONTENT_OWNER,
            report_month="2026-03",
            dry_run=False,
            triggered_by_user_id=None,
            actor_identity=ACTOR,
        )
        sync = executor.submit_group_sync_if_absent(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert pull is not None
        assert sync is not None
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id=CONTENT_OWNER,
                report_month="2026-03",
            )
            is True
        )
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key=GROUP_SYNC_JOB_CONNECTOR_SLUG,
                account_id=CONTENT_OWNER,
                report_month=GROUP_SYNC_JOB_MONTH,
            )
            is True
        )
        executor.cancel_reservation(pull)
        executor.cancel_reservation(sync)
    finally:
        executor.close()


def test_submit_group_sync_if_absent_dedups_then_frees_after_completion(
    tmp_path: Path, service_actor: str, resolves_ok: None
) -> None:
    """A second submit returns None while the first is live, and succeeds once it completes."""
    factory = _factory(tmp_path)
    executor = _executor(
        factory,
        client_factory=lambda _c: _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)]),
    )
    try:
        first = executor.submit_group_sync_if_absent(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert first is not None
        assert (
            executor.submit_group_sync_if_absent(
                tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
            )
            is None
        )
        executor.activate(first).result(timeout=10)
        assert (
            executor.has_active_job(
                tenant_id=TENANT,
                connector_key=GROUP_SYNC_JOB_CONNECTOR_SLUG,
                account_id=CONTENT_OWNER,
                report_month=GROUP_SYNC_JOB_MONTH,
            )
            is False
        )
        third = executor.submit_group_sync_if_absent(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert third is not None
        executor.cancel_reservation(third)
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# Worker happy path + one-transaction atomicity
# ---------------------------------------------------------------------------


def test_worker_applies_new_group_and_writes_all_rows(
    tmp_path: Path, service_actor: str, resolves_ok: None
) -> None:
    """A snapshot with one new group lands the group, membership, and audit rows."""
    factory = _factory(tmp_path)
    fake = _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    executor = _executor(factory, client_factory=lambda _c: fake)
    try:
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()

    with factory() as session:
        group = session.scalars(
            select(ChannelGroupORM).where(ChannelGroupORM.cms_group_id == "cms-a")
        ).one()
        assert group.name == "News"
        assert group.content_owner_id == CONTENT_OWNER
        members = session.scalars(
            select(ChannelGroupMemberORM).where(ChannelGroupMemberORM.group_id == group.id)
        ).all()
        assert len(members) == 1

    audits = _audit_rows(factory)
    event_types = [a.event_type for a in audits]
    assert AuditEventType.GROUP_UPDATED.value in event_types
    summaries = [a for a in audits if a.event_type == AuditEventType.GROUPS_SYNCED.value]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.entity_type == "channel_group_sync"
    assert summary.entity_id == CONTENT_OWNER
    assert summary.reason == "scheduled CMS group sync"
    assert summary.details["content_owner_id"] == CONTENT_OWNER
    assert summary.details["counts"]["CREATE"] == 1
    assert fake.closed is True


def test_worker_rolls_back_domain_and_audit_together_on_summary_failure(
    tmp_path: Path, service_actor: str, resolves_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after apply but before the single commit discards group AND audit rows.

    The #169 atomic invariant, proven the way the route suite's mid-apply test
    does: the domain rows and their audit rows share the worker's one
    transaction, so a failure before the lone commit leaves nothing behind. Here
    the run-level GROUPS_SYNCED summary write raises after apply has already
    flushed the group row and its GROUP_UPDATED row; both must vanish.
    """
    factory = _factory(tmp_path)
    executor = _executor(
        factory,
        client_factory=lambda _c: _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)]),
    )

    def _boom_on_summary(*, event_type: AuditEventType, **kwargs: object) -> object:
        if event_type is AuditEventType.GROUPS_SYNCED:
            raise RuntimeError("summary audit boom")
        return _real_record(event_type=event_type, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "ums_smart_revenue.connectors.runs.executor.record_audit_event", _boom_on_summary
    )

    try:
        # Fail-closed: the worker must swallow the error, not re-raise it.
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()

    with factory() as session:
        groups = session.scalars(
            select(ChannelGroupORM).where(ChannelGroupORM.cms_group_id == "cms-a")
        ).all()
    assert groups == []
    assert _audit_rows(factory) == []


# ---------------------------------------------------------------------------
# UNCHANGED-only writes nothing (with its anti-vacuity twin)
# ---------------------------------------------------------------------------


def test_worker_converged_writes_zero_rows_then_a_change_writes_rows(
    tmp_path: Path, service_actor: str, resolves_ok: None
) -> None:
    """A converged re-tick writes ZERO audit rows; a renamed snapshot then writes rows.

    The twin is the anti-vacuity guard: without it, "zero rows on re-tick" could
    pass simply because the worker never writes anything at all.
    """
    factory = _factory(tmp_path)
    snapshot_box: list[_Snapshot] = [[("cms-a", "News", (CHANNEL_ONE,), 0)]]
    executor = _executor(
        factory, client_factory=lambda _c: _FakeGroupsClient(list(snapshot_box[0]))
    )
    try:
        # Run 1: creates the group and writes its rows.
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        after_create = len(_audit_rows(factory))
        assert after_create > 0

        # Run 2: identical snapshot -> everything UNCHANGED -> no new audit rows.
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert len(_audit_rows(factory)) == after_create

        # Twin: a rename DOES write rows, so the zero above is not vacuous.
        snapshot_box[0] = [("cms-a", "News HD", (CHANNEL_ONE,), 0)]
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert len(_audit_rows(factory)) > after_create
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# Failure taxonomy: one group_sync_job_failed row per typed family
# ---------------------------------------------------------------------------


def _one_failure_row(factory: sessionmaker) -> AuditLogORM:
    """Return the single CONNECTOR_JOB_RUN group_sync_job_failed row."""
    rows = [a for a in _audit_rows(factory) if a.details.get("action") == "group_sync_job_failed"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.event_type == AuditEventType.CONNECTOR_JOB_RUN.value
    assert row.entity_id == f"{GROUP_SYNC_JOB_CONNECTOR_SLUG}:{CONTENT_OWNER}"
    assert row.details["content_owner_id"] == CONTENT_OWNER
    return row


def test_worker_missing_credential_audits_failure(tmp_path: Path, service_actor: str) -> None:
    """No credential row -> the real resolver raises CredentialNotFoundError -> one audit row."""
    factory = _factory(tmp_path, seed_channel=False)
    executor = _executor(factory, client_factory=lambda _c: _FakeGroupsClient([]))
    try:
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()
    assert _one_failure_row(factory).details["error_class"] == "CredentialNotFoundError"


def test_worker_fetch_failure_audits_failure(
    tmp_path: Path, service_actor: str, resolves_ok: None, caplog
) -> None:
    """A fetch-phase GoogleConnectorError surfaces as GroupSyncFetchError -> one audit row."""
    factory = _factory(tmp_path)
    fake = _FakeGroupsClient(
        [("cms-a", "News", (CHANNEL_ONE,), 0)],
        groups_error=GoogleApiResponseError(
            url=(
                "https://alice:password@example.test/groups"
                "?X-Goog-Signature=signed-secret"
            ),
            reason="Authorization: Bearer bearer-secret",
        ),
    )
    executor = _executor(factory, client_factory=lambda _c: fake)
    try:
        with caplog.at_level(
            "ERROR", logger="ums_smart_revenue.connectors.runs.executor"
        ):
            executor._run_group_sync_job(
                tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
            )
    finally:
        executor.close()
    assert _one_failure_row(factory).details["error_class"] == "GroupSyncFetchError"
    expected = [
        record
        for record in caplog.records
        if "Scheduled group sync failed" in record.getMessage()
    ]
    assert len(expected) == 1
    assert expected[0].exc_info is None
    assert "failure_category=group_sync_fetch_failure" in expected[0].getMessage()
    assert "signed-secret" not in expected[0].getMessage()
    assert "bearer-secret" not in expected[0].getMessage()


def test_worker_conflict_refusal_audits_failure(
    tmp_path: Path, service_actor: str, resolves_ok: None
) -> None:
    """A CMS key already owned by a DIFFERENT owner refuses -> one audit row."""
    factory = _factory(tmp_path)
    with factory() as session:
        session.add(
            ChannelGroupORM(
                id=uuid4(),
                tenant_id=TENANT,
                name="Owner B Sector",
                group_type="SECTOR",
                cms_group_id="cms-a",
                content_owner_id=OTHER_OWNER,
                active=True,
            )
        )
        session.commit()
    executor = _executor(
        factory,
        client_factory=lambda _c: _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)]),
    )
    try:
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()
    assert _one_failure_row(factory).details["error_class"] == "GroupSyncConflictRefusedError"


def test_worker_suspended_tenant_audits_failure(tmp_path: Path, service_actor: str) -> None:
    """A non-ACTIVE tenant raises TenantLifecycleError; the fresh-session audit still lands.

    Proves the new failure path's platform-lane + placeholder-tenant RLS bridge:
    the failure audit must NOT re-enter connector_tenant_context (which would
    raise the same lifecycle error before the row could be written).
    """
    factory = _factory(tmp_path, tenant_status=TenantStatus.SUSPENDED)
    executor = _executor(factory, client_factory=lambda _c: _FakeGroupsClient([]))
    try:
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()
    assert _one_failure_row(factory).details["error_class"] == "TenantLifecycleError"


def test_worker_unconfigured_service_actor_audits_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset service-actor env is a typed pre-start failure, not a swallowed ValueError."""
    monkeypatch.delenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, raising=False)
    load_app_settings.cache_clear()
    factory = _factory(tmp_path)
    executor = _executor(factory, client_factory=lambda _c: _FakeGroupsClient([]))
    try:
        executor._run_group_sync_job(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
    finally:
        executor.close()
    assert (
        _one_failure_row(factory).details["error_class"]
        == "ConnectorServicePrincipalUnavailableError"
    )


# ---------------------------------------------------------------------------
# Shutdown: a queued-but-cancelled sync job is audited by the shared path
# ---------------------------------------------------------------------------


def test_close_audits_queued_sync_job_with_before_start_row(
    tmp_path: Path, service_actor: str, resolves_ok: None
) -> None:
    """A sync job cancelled at shutdown is audited kind-agnostically (task #6 decision).

    The single worker is occupied by a slow pull, so the activated sync job sits
    in the queue. close() cancels it and audits it through the shared
    ``_audit_failed_before_start`` path: action="job_failed_before_start",
    error_class="ExecutorShutdown". The row is still attributable to the sync job
    by its connector-key-bearing entity_id and the report_month="-" sentinel, so
    re-tagging it with the run-time group_sync_job_failed taxonomy would only
    mislabel a job that never started.
    """
    factory = _factory(tmp_path)
    executor = _executor(
        factory,
        client_factory=lambda _c: _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)]),
    )
    started = threading.Event()

    def _slow_run_one(session: object, **kwargs: object) -> ConnectorRunOutcome:
        started.set()
        time.sleep(0.5)
        return ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])

    try:
        with patch("ums_smart_revenue.connectors.runs.executor.run_one", _slow_run_one):
            pull = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            reservation = executor.submit_group_sync_if_absent(
                tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
            )
            assert reservation is not None
            sync_future = executor.activate(reservation)
            started.wait(timeout=5)
            executor.close()
            _ = pull, sync_future
    finally:
        executor.close()

    shutdown = [
        a for a in _audit_rows(factory) if a.details.get("error_class") == "ExecutorShutdown"
    ]
    assert len(shutdown) == 1
    assert shutdown[0].entity_id == f"{GROUP_SYNC_JOB_CONNECTOR_SLUG}:{CONTENT_OWNER}"
    assert shutdown[0].details["action"] == "job_failed_before_start"
    assert shutdown[0].details["report_month"] == GROUP_SYNC_JOB_MONTH


def test_activate_enqueue_failure_drops_reservation(tmp_path: Path) -> None:
    """A pool refusing the worker must NOT wedge the slot as in-flight forever.

    Qodo PR-171 finding ("wedge on activate exception"): if
    ``ThreadPoolExecutor.submit`` raises inside ``activate`` (e.g. shutdown
    raced the enqueue), the reservation used to stay in the registry, so
    every later scheduled sync for that (tenant, owner) was dedup-skipped
    until process restart -- a transient failure became a permanent liveness
    failure. The fix is compare-and-delete cleanup inside ``activate``
    itself: OUR reservation is dropped while the lock is held, the exception
    still propagates for the caller to log, and the next tick can claim the
    slot again.
    """
    factory = _factory(tmp_path)
    executor = _executor(factory)
    try:
        reservation = executor.submit_group_sync_if_absent(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert reservation is not None

        with (
            patch.object(
                executor._executor,
                "submit",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            pytest.raises(RuntimeError, match="cannot schedule new futures"),
        ):
            executor.activate(reservation)

        # The wedge, before the fix: the key was still in the registry here.
        assert not executor.has_active_job(
            tenant_id=TENANT,
            connector_key=GROUP_SYNC_JOB_CONNECTOR_SLUG,
            account_id=CONTENT_OWNER,
            report_month=GROUP_SYNC_JOB_MONTH,
        )

        # The retry path: the slot is claimable again immediately.
        retry = executor.submit_group_sync_if_absent(
            tenant_id=TENANT, content_owner_id=CONTENT_OWNER, actor_identity=ACTOR
        )
        assert retry is not None
        # No real worker needed for the assertion; release the slot cleanly.
        assert executor.cancel_reservation(retry) is True
    finally:
        executor.close()


def test_failure_audits_swallow_actor_construction_error(tmp_path: Path) -> None:
    """A broken audit-actor build must NOT escape the failure handlers.

    Qodo PR-171 finding ("exception escape"): the executor-owned audit writers
    used to build the actor BEFORE their best-effort guard, so a construction
    failure escaped the worker's "never propagate" contract -- naked from the
    group-sync worker's except block, and into route transaction hooks via the
    public ``audit_failed_before_start``. All three now construct INSIDE the
    guard: a broken build degrades to a logged skip with zero rows written.
    """
    factory = _factory(tmp_path)
    executor = _executor(factory)
    outcome = ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])
    try:
        with patch.object(
            ConnectorJobExecutor,
            "_build_audit_actor",
            side_effect=RuntimeError("actor build exploded"),
        ):
            # The Qodo-flagged group-sync failure handler ...
            executor._audit_group_sync_failure(
                tenant_id=TENANT,
                content_owner_id=CONTENT_OWNER,
                error_class="GroupSyncFetchError",
                actor_identity=ACTOR,
            )
            # ... its pull-job sibling via the public after_commit hook ...
            executor.audit_failed_before_start(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                error_class="ExecutorShutdown",
                actor_identity=ACTOR,
            )
            # ... and the dry-run outcome writer: none may raise.
            executor._audit_dry_run_outcome(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                outcome=outcome,
                actor_identity=ACTOR,
            )
        assert _audit_rows(factory) == []
    finally:
        executor.close()
