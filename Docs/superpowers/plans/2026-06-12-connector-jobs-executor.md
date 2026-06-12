# Connector Jobs Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL — before executing any task below, read and
> follow `Docs/superpowers/skills/executing-plans` (TDD discipline: write the failing test
> first, run it to confirm the expected failure, write the minimal implementation, run it to
> green, then commit). Do the tasks in order; later tasks depend on earlier ones. Every commit
> message MUST be trailer-free (no `Co-Authored-By`, no "Generated with" footer).

**Goal**

Turn `POST /connectors/jobs` from a no-op recorder (returns the literal
`execution_status: "recorded_not_executed"`, never calls `run_one`) into an executing path that
submits a real Google ingest pull to a module-owned, bounded, in-process
`ConnectorJobExecutor` and returns **202 `submitted`** immediately. The worker thread runs the
proven CLI pattern on its own session
(`build_session_factory()()` -> `connector_tenant_context(tenant_id, session=session)` ->
`run_one(...)`). Add a duplicate-run guard (in-process registry + a DB RUNNING-row reader with
orphan-supersede), a fail-closed `connector_job_executor_enabled` setting (default OFF -> 503),
a thin frontend "Run pull" control, and **Part 2**: four additive `api_connector_credentials`
refresh-telemetry columns stamped at the single `resolve_connector_credentials` chokepoint.

**Architecture**

- Route (`api/connectors.py`) stays thin: authz -> 503 disabled -> 422 unknown-key -> 422
  bad-month -> 422 missing/inactive cred -> 409 dup / orphan-supersede -> 202 submit. Writes
  exactly **one** route-owned audit row (`job_submitted` / `job_rejected`). `run_one` later
  emits its own STARTED/FINISHED `CONNECTOR_JOB_RUN` edges on the worker's session.
- Executor (`connectors/runs/executor.py`) owns a `ThreadPoolExecutor(max_workers, ...)`, a
  `threading.Lock`-guarded in-process registry keyed `(tenant, key, account, month)`, a
  `weakref.finalize` GC backstop, and an explicit `close()` — mirroring
  `tenancy/resolver.py:110-127`. The worker never wraps `run_one` in `platform_lane` (not
  nest-safe; `run_one` manages its own elevation internally).
- App wiring (`app.py`) constructs the executor inside `if resolved_database_url:` only when
  enabled (default OFF, so `app = create_app()` at import spawns no threads), attaches it to
  `app.state.connector_job_executor`, and closes it from a new FastAPI `lifespan` shutdown.
- Part 2 schema is additive (one Alembic migration off the single linear head
  `20260609_0002`; four nullable columns + a CHECK on `last_refresh_status`). The
  `api_connector_credentials` table is tenant-scoped/tenant-writable, so no RLS grant-pin
  impact and no `platform_lane` needed for the telemetry UPDATE.

**Tech Stack**

Python 3 / FastAPI / SQLAlchemy (ORM + Alembic) / PostgreSQL (RLS, source of truth) with a
SQLite test tier; `concurrent.futures.ThreadPoolExecutor` + `threading` + `weakref` for the
worker; React + TypeScript + Vitest for the frontend; pytest for backend tests. DeepSource
`FLK-E501` enforces **<= 100 chars** on every touched Python line (the 120 in
`.deepsource.toml` is ignored) — keep migration/test lines <= 100 too.

---

### Task 1: Settings — three executor config fields

**Files:**
- Modify: `backend/ums_smart_revenue/config/settings.py`
  - env-name constants after line 11 (`GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV`)
  - three fields on the frozen `AppSettings` dataclass after line 35
    (`google_connector_service_actor_id`)
  - constructor wiring in `load_app_settings()` `AppSettings(...)` call at lines 54-59
  - new `_load_bool` / `_load_int` helpers after `_load_google_connector_service_actor_id`
    (current end line 116), mirroring its two-tier "missing -> default / malformed ->
    ValueError carrying the env name" contract
- Test: `tests/config/test_settings.py` (existing module; autouse `reset_app_settings_cache`
  in `tests/conftest.py:49-56` already clears the cache around each test)

Steps:

- [ ] Write the failing tests. Append to `tests/config/test_settings.py`:

```python
from ums_smart_revenue.config.settings import (
    CONNECTOR_JOB_EXECUTOR_ENABLED_ENV,
    CONNECTOR_JOB_MAX_WORKERS_ENV,
    CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV,
)

_EXECUTOR_ENVS = (
    CONNECTOR_JOB_EXECUTOR_ENABLED_ENV,
    CONNECTOR_JOB_MAX_WORKERS_ENV,
    CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV,
)


def _clear_executor_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every executor env var so defaults apply."""
    for name in _EXECUTOR_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_load_app_settings_executor_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset executor envs resolve to the fail-closed defaults."""
    _clear_executor_envs(monkeypatch)
    load_app_settings.cache_clear()
    settings = load_app_settings()
    assert settings.connector_job_executor_enabled is False
    assert settings.connector_job_max_workers == 1
    assert settings.connector_job_stale_running_hours == 6


def test_load_app_settings_executor_valid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid executor env values override the defaults."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, "true")
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "4")
    monkeypatch.setenv(CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, "12")
    load_app_settings.cache_clear()
    settings = load_app_settings()
    assert settings.connector_job_executor_enabled is True
    assert settings.connector_job_max_workers == 4
    assert settings.connector_job_stale_running_hours == 12


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "  yes  ", "on"])
def test_load_app_settings_executor_enabled_truthy(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    """Recognised truthy tokens enable the executor."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, truthy)
    load_app_settings.cache_clear()
    assert load_app_settings().connector_job_executor_enabled is True


@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "  no  ", "off", ""])
def test_load_app_settings_executor_enabled_falsy(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Recognised falsy/blank tokens leave the executor disabled."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, falsy)
    load_app_settings.cache_clear()
    assert load_app_settings().connector_job_executor_enabled is False


def test_load_app_settings_rejects_malformed_max_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer max-workers value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "not-a-number")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_MAX_WORKERS_ENV in str(excinfo.value)


def test_load_app_settings_rejects_zero_max_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive max-workers value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "0")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_MAX_WORKERS_ENV in str(excinfo.value)


def test_load_app_settings_rejects_zero_stale_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive stale-running-hours value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, "-3")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV in str(excinfo.value)
```

- [ ] Run to fail:
  `python -m pytest tests/config/test_settings.py -q`
  Expected: `ImportError: cannot import name 'CONNECTOR_JOB_EXECUTOR_ENABLED_ENV' ...`
  (the constants and fields do not exist yet).

- [ ] Minimal implementation. In `backend/ums_smart_revenue/config/settings.py`, add the env
  constants after line 11:

```python
CONNECTOR_JOB_EXECUTOR_ENABLED_ENV = "UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"
CONNECTOR_JOB_MAX_WORKERS_ENV = "UMS_CONNECTOR_JOB_MAX_WORKERS"
CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV = "UMS_CONNECTOR_JOB_STALE_RUNNING_HOURS"

_TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSY_TOKENS = frozenset({"0", "false", "no", "off", ""})
```

  Add the three fields to the `AppSettings` dataclass after line 35
  (`google_connector_service_actor_id: str | None = None`):

```python
    # In-process connector-job executor toggle + tuning. Fail-closed OFF:
    # when False, POST /connectors/jobs returns 503 (explicit refusal, never a
    # silent fallback to the old recorder). max_workers / stale_running_hours
    # are positive ints validated at load time so a typo fails closed at boot.
    connector_job_executor_enabled: bool = False
    connector_job_max_workers: int = 1
    connector_job_stale_running_hours: int = 6
```

  Wire them into the `AppSettings(...)` call in `load_app_settings()` (the existing call ends
  at line 59 with `google_connector_service_actor_id=...`); add three keyword arguments:

```python
        google_connector_service_actor_id=_load_google_connector_service_actor_id(),
        connector_job_executor_enabled=_load_bool(
            CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, default=False
        ),
        connector_job_max_workers=_load_int(
            CONNECTOR_JOB_MAX_WORKERS_ENV, default=1
        ),
        connector_job_stale_running_hours=_load_int(
            CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, default=6
        ),
```

  Add the two helpers after `_load_google_connector_service_actor_id` (after line 116):

```python
# ============================================================================
# Purpose: Parse a UMS_* boolean env var at settings-load time with a two-tier
#          contract: missing/blank -> default; recognised truthy/falsy token ->
#          the matching bool; unrecognised token -> ValueError carrying the env
#          name so a misconfigured deployment fails fast at boot.
# Database/ORM: None.
# Standards: Mirrors _load_google_connector_service_actor_id's "missing ->
#            default vs malformed -> fail-closed" idiom; case/whitespace
#            insensitive.
# Blast Radius: Connector-job executor enablement (route 503 vs submit). No
#               finance, audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> create_app gates the executor
#     construction on connector_job_executor_enabled.
# ============================================================================
def _load_bool(env_name: str, *, default: bool) -> bool:
    """Parse a UMS_* boolean env var, failing fast on an unrecognised token."""
    raw = environ.get(env_name)
    if raw is None:
        return default
    candidate = raw.strip().lower()
    if candidate in _TRUTHY_TOKENS:
        return True
    if candidate in _FALSY_TOKENS:
        return False
    raise ValueError(
        f"{env_name} must be one of: "
        f"{', '.join(sorted(_TRUTHY_TOKENS | _FALSY_TOKENS - {''}))}"
    )


# ============================================================================
# Purpose: Parse a UMS_* positive-int env var at settings-load time with a
#          two-tier contract: missing/blank -> default; present-but-malformed
#          or non-positive -> ValueError carrying the env name (a 0 or negative
#          worker count would build a broken ThreadPoolExecutor / stale window).
# Database/ORM: None.
# Standards: Mirrors the UUID loader's fail-closed-at-boot idiom; rejects <= 0.
# Blast Radius: Executor pool size + orphan-supersede age threshold. No finance,
#               audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> consumes
#     max_workers; the route consumes stale_running_hours.
# ============================================================================
def _load_int(env_name: str, *, default: int) -> int:
    """Parse a UMS_* positive-int env var, failing fast on bad/non-positive."""
    raw = environ.get(env_name)
    if raw is None:
        return default
    candidate = raw.strip()
    if not candidate:
        return default
    try:
        parsed = int(candidate)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{env_name} must be a positive integer")
    return parsed
```

- [ ] Run to pass:
  `python -m pytest tests/config/test_settings.py -q`
  Expected: all settings tests pass (existing + the 8 new).

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/config/settings.py tests/config/test_settings.py
  git commit -m "feat(config): connector-job executor settings (enabled/max_workers/stale_hours)"
  ```

---

### Task 2: Executor module (`ConnectorJobExecutor`)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/runs/executor.py`
- Test: `tests/connectors/runs/test_executor.py` (new; SQLite — call `_run_job` directly or
  submit then `future.result()` to avoid thread races)

Reuses: `build_session_factory` (`db/session.py:82-112`),
`connector_tenant_context` (`connectors/runs/tenant_context.py:73-178`),
`run_one` (`connectors/runs/orchestrator.py:383-425`),
`GoogleConnectorError` (`connectors/google/errors.py:13`),
`SqlAlchemyAuditSink` (`auth/sql_audit_sink.py:14`),
`record_audit_event` (`auth/audit_service.py:41`),
`AuditEventType.CONNECTOR_JOB_RUN` (`auth/audit.py:36`),
the resolver executor pattern (`tenancy/resolver.py:110-127`).

Steps:

- [ ] Write the failing test. Create `tests/connectors/runs/test_executor.py`:

```python
"""Unit tests for the in-process ConnectorJobExecutor worker + registry."""
from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.errors import OAuthRefreshError
from ums_smart_revenue.connectors.runs.executor import (
    ConnectorJobActor,
    ConnectorJobExecutor,
)
from ums_smart_revenue.connectors.runs.orchestrator import ConnectorRunOutcome
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import (
    AuditLogORM,
    SecurityBase,
    UserORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

TENANT = UUID(UMS_TENANT_ID)
ACTOR = ConnectorJobActor(
    user_id=str(uuid4()), email="ops@example.com", role="revenue_operations_admin"
)


def _factory(tmp_path) -> sessionmaker:
    url = f"sqlite+pysqlite:///{(tmp_path / 'exec.db').as_posix()}"
    engine = create_engine(url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=UUID(ACTOR.user_id), email=ACTOR.email))
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _outcome() -> ConnectorRunOutcome:
    return ConnectorRunOutcome(run=None, counts={}, per_report_failures=[])


def test_run_job_uses_own_session_and_sets_tenant_context(tmp_path) -> None:
    """The worker opens its own session and TENANT_CTX is set inside run_one."""
    factory = _factory(tmp_path)
    seen: dict[str, object] = {}

    def _fake_run_one(session, **kwargs):
        tenant = get_current_tenant()
        seen["tenant_id"] = None if tenant is None else tenant.id
        seen["session_is_factory"] = isinstance(session, Session)
        return _outcome()

    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _fake_run_one
        ):
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
    finally:
        executor.close()

    assert seen["tenant_id"] == TENANT
    assert seen["session_is_factory"] is True
    # TENANT_CTX is reset after the worker exits (no leak into this thread).
    assert get_current_tenant() is None


def test_run_job_removes_registry_entry_on_success(tmp_path) -> None:
    """A successful run clears its registry key in finally."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()


def test_run_job_bucket_a_failure_writes_audit_and_does_not_propagate(
    tmp_path,
) -> None:
    """A Bucket-A GoogleConnectorError is caught, audited, never re-raised."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            executor._register(key)
            # Must NOT raise out of the worker body.
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
    assert row.event_type == "CONNECTOR_JOB_RUN"
    assert row.details["action"] == "job_failed_before_start"
    assert row.details["error_class"] == "OAuthRefreshError"
    # Canned class name only — never the exception text.
    assert "revoked" not in str(row.details)


def test_run_job_unexpected_exception_swallowed_and_registry_cleared(
    tmp_path,
) -> None:
    """A projection-style re-raise is swallowed; the registry key is cleared."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    key = (TENANT, "youtube_reporting", "acct-1", "2026-03")

    def _boom(session, **kwargs):
        raise RuntimeError("projection failed; run already FAILED+audited")

    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one", _boom
        ):
            executor._register(key)
            executor._run_job(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()

    # An unexpected (non-Bucket-A) error logs but writes NO job_failed audit.
    with factory() as session:
        assert session.scalars(select(AuditLogORM)).all() == []


def test_submit_then_future_result_clears_active_flag(tmp_path) -> None:
    """submit() registers the key, runs the worker, and clears it on completion."""
    factory = _factory(tmp_path)
    executor = ConnectorJobExecutor(
        session_factory=factory, max_workers=1, stale_running_hours=6
    )
    try:
        with patch(
            "ums_smart_revenue.connectors.runs.executor.run_one",
            lambda session, **kw: _outcome(),
        ):
            future = executor.submit(
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                report_month="2026-03",
                dry_run=False,
                triggered_by_user_id=None,
                actor_identity=ACTOR,
            )
            future.result(timeout=10)
        assert executor.has_active_job(
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
            report_month="2026-03",
        ) is False
    finally:
        executor.close()
```

  (Note: `submit` returns the `Future` so the test can join deterministically; the route
  ignores the return value, matching the spec's "submit -> None" intent at the call site.)

  IMPORTANT setup caveat: the worker enters `connector_tenant_context(tenant_id,
  session=session)` — the PRODUCTION path, which loads the tenant via
  `SqlAlchemyTenantRepository.get_by_id` and raises `TenantLifecycleError` (a
  `GoogleConnectorError`, so it would be swallowed by the Bucket-A catch) unless an **ACTIVE**
  tenant row exists. `_factory` MUST therefore also create the tenancy table and seed an ACTIVE
  tenant for `UMS_TENANT_ID` (grep `tenants`/`TenantORM`/`TenantStatus` and mirror how
  `tests/connectors/runs/test_tenant_context.py` or `test_run_one_rls_postgres.py` seeds an
  ACTIVE tenant). If the first run-to-fail shows `TenantLifecycleError`/empty `seen`, this seed
  is the cause — add it before re-running.

- [ ] Run to fail:
  `python -m pytest tests/connectors/runs/test_executor.py -q`
  Expected: `ModuleNotFoundError: No module named 'ums_smart_revenue.connectors.runs.executor'`.

- [ ] Minimal implementation. Create
  `backend/ums_smart_revenue/connectors/runs/executor.py`:

```python
"""In-process bounded executor that runs connector pulls off the request thread."""
from __future__ import annotations

import logging
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.connectors.runs.orchestrator import run_one
from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.session import SessionFactory

logger = logging.getLogger(__name__)

_JobKey = tuple[UUID, str, str, str]


@dataclass(frozen=True)
class ConnectorJobActor:
    """Minimal, thread-safe snapshot of the submitting principal for the worker.

    The worker thread cannot share the request's UserPrincipal safely across the
    thread boundary, so the route passes this immutable snapshot and the worker
    rebuilds a UserPrincipal carrying RUN_CONNECTOR_JOBS@global for the
    Bucket-A failure audit (attribution preserved via the audit reason + the
    sink's unknown-actor stash in details['actor_user_id']).
    """

    user_id: str
    email: str
    role: str


# ============================================================================
# Purpose: Own a bounded ThreadPoolExecutor + an in-process registry of live
#   jobs keyed (tenant, connector_key, account_id, report_month), and run each
#   submitted connector pull on its OWN session under connector_tenant_context
#   (re-establishing TENANT_CTX in the worker thread, which does not inherit the
#   request contextvar). Mirrors the TenantResolverMiddleware executor pattern
#   (weakref.finalize GC backstop + explicit close()).
# Database/ORM: opens its own Session via session_factory; run_one writes
#   connector_runs + audit_logs; the Bucket-A catch writes one CONNECTOR_JOB_RUN
#   audit row via SqlAlchemyAuditSink on a fresh own session, wrapped in
#   platform_lane (audit_logs is a TENANT_PLATFORM_ONLY_WRITE table -> a
#   tenant-lane write would InsufficientPrivilege-deny on Postgres; SQLite no-op).
# Standards: never wraps run_one in platform_lane (not nest-safe; run_one owns
#   its OWN elevation internally) -- platform_lane is used ONLY for the separate
#   Bucket-A audit that runs OUTSIDE run_one. Worker NEVER propagates out of the
#   thread: Bucket-A errors are audited (canned class name only), everything else
#   is logged. Registry key removed in finally on every path.
# Blast Radius: Authorization (tenant-pinned worker), audit (additive
#   job_failed_before_start), connector run lifecycle. No finance math change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> executor +
#     weakref.finalize + close() precedent.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     connector_tenant_context replays the ACTIVE-only tenant gate.
#   - File: scripts/run_google_connector.py -> the CLI pattern this reuses.
# ============================================================================
class ConnectorJobExecutor:
    """Bounded in-process runner for connector pull jobs with a dup registry."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        max_workers: int,
        stale_running_hours: int,
    ) -> None:
        """Build the pool, the registry lock, and the GC-safe shutdown backstop."""
        self._session_factory = session_factory
        self._stale_running_hours = stale_running_hours
        self._lock = threading.Lock()
        self._registry: dict[_JobKey, Future] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ums-connector-job",
        )
        self._finalizer = weakref.finalize(
            self,
            self._executor.shutdown,
            wait=False,
            cancel_futures=True,
        )

    def close(self) -> None:
        """Shut the pool down deterministically (called from the app lifespan)."""
        self._finalizer()

    def has_active_job(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
    ) -> bool:
        """Return whether a live Future exists for the exact scope (under lock)."""
        key = (tenant_id, connector_key, account_id, report_month)
        with self._lock:
            return key in self._registry

    def _register(self, key: _JobKey) -> None:
        """Reserve a registry slot before submission (caller holds no lock)."""
        with self._lock:
            self._registry[key] = Future()

    def _deregister(self, key: _JobKey) -> None:
        """Drop a registry slot on worker completion."""
        with self._lock:
            self._registry.pop(key, None)

    def submit(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        dry_run: bool,
        triggered_by_user_id: UUID | None,
        actor_identity: ConnectorJobActor,
    ) -> Future:
        """Register the scope and submit the pull to the worker pool."""
        key = (tenant_id, connector_key, account_id, report_month)
        # Register the REAL future atomically under the lock: enqueue while
        # holding the lock so a fast worker's finally->_deregister blocks until
        # this entry is set, then pops it. The previous register-placeholder ->
        # submit -> overwrite-after sequence had a race: a worker that finished
        # and deregistered BEFORE the overwrite would have a completed future
        # re-inserted, wedging has_active_job at True forever. ThreadPoolExecutor
        # .submit only enqueues (never blocks on a full pool), so holding the
        # lock across it is brief and deadlock-free.
        with self._lock:
            future = self._executor.submit(
                self._run_job,
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                dry_run=dry_run,
                triggered_by_user_id=triggered_by_user_id,
                actor_identity=actor_identity,
            )
            self._registry[key] = future
        return future

    def _run_job(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        dry_run: bool,
        triggered_by_user_id: UUID | None,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Worker body: own session -> tenant context -> run_one; fail-closed."""
        key = (tenant_id, connector_key, account_id, report_month)
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    run_one(
                        session,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        account_id=account_id,
                        report_month=report_month,
                        dry_run=dry_run,
                        triggered_by_user_id=triggered_by_user_id,
                    )
        except GoogleConnectorError as exc:
            logger.exception(
                "Connector job failed before start (tenant=%s connector=%s)",
                tenant_id,
                connector_key,
            )
            self._audit_failed_before_start(
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                error_class=type(exc).__name__,
                actor_identity=actor_identity,
            )
        except Exception:  # noqa: BLE001 — fail-closed: never escape the thread
            logger.exception(
                "Connector job worker raised after start (tenant=%s connector=%s)",
                tenant_id,
                connector_key,
            )
        finally:
            self._deregister(key)

    def _audit_failed_before_start(
        self,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
        report_month: str,
        error_class: str,
        actor_identity: ConnectorJobActor,
    ) -> None:
        """Write ONE CONNECTOR_JOB_RUN job_failed_before_start row, fresh session."""
        actor = UserPrincipal(
            user_id=actor_identity.user_id,
            email=actor_identity.email,
            direct_permissions=(
                PermissionGrant(
                    permission=Permission.RUN_CONNECTOR_JOBS,
                    scope=AccessScope.global_scope(),
                    active=True,
                ),
            ),
            tenant_id=str(tenant_id),
        )
        try:
            with self._session_factory() as session:
                with connector_tenant_context(tenant_id, session=session):
                    # audit_logs is platform-only-write: elevate to app_platform
                    # for this standalone audit (run_one does its own elevation;
                    # this audit runs OUTSIDE run_one). No-op off Postgres.
                    with platform_lane(session):
                        sink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
                        record_audit_event(
                            sink=sink,
                            actor=actor,
                            event_type=AuditEventType.CONNECTOR_JOB_RUN,
                            entity_type="api_connector",
                            entity_id=f"{connector_key}:{account_id}",
                            scope=AccessScope.connector(connector_key),
                            reason="connector job failed before start",
                            details={
                                "action": "job_failed_before_start",
                                "report_month": report_month,
                                "error_class": error_class,
                            },
                        )
                        session.commit()
        except Exception:  # noqa: BLE001 — best-effort audit, never escape
            logger.exception(
                "Failed to persist job_failed_before_start audit (tenant=%s)",
                tenant_id,
            )
```

  Confirm `SessionFactory` is importable from `db/session.py` (it is the alias used by
  `tenancy/resolver.py` and `app.py`); if it is not exported there, import it from
  `ums_smart_revenue.tenancy.resolver import SessionFactory` instead. Keep all lines <= 100.

- [ ] Run to pass:
  `python -m pytest tests/connectors/runs/test_executor.py -q`
  Expected: all six executor tests pass.

- [ ] Write the PG-tier RLS proof (SQLite cannot exercise `platform_lane`; this is the #94
  lesson — a tenant-lane write to `audit_logs` is a no-op on SQLite but `InsufficientPrivilege`
  on RLS-enforced Postgres). Create `tests/connectors/runs/test_executor_rls_postgres.py` by
  adapting the harness in the EXISTING `tests/connectors/runs/test_run_one_rls_postgres.py`
  (read it for the exact fixture pattern: `require_postgres_url`, `reset_public_schema`,
  `command.upgrade(cfg, "head")`, the app_tenant/app_platform role setup, and the ACTIVE tenant
  + user + `ApiConnectorCredentialORM` seeding). The single scenario:
  - Build the executor with a real PG tenant-lane `build_session_factory(postgres_url)`.
  - Patch `ums_smart_revenue.connectors.runs.executor.run_one` to raise
    `OAuthRefreshError(inner=RuntimeError("revoked"))` (Bucket A).
  - Call `executor._run_job(...)` (synchronous; no thread) for an ACTIVE tenant/credential.
  - Assert a `CONNECTOR_JOB_RUN` row with `details["action"]=="job_failed_before_start"` and
    `details["error_class"]=="OAuthRefreshError"` IS present in `audit_logs` (read on a platform
    or fresh session) — i.e. the `platform_lane` elevation let the tenant-lane worker persist a
    platform-only-write audit row. Assert `"revoked"` does not appear in the details.
  This proves the platform_lane fix; without it the audit write would deny and the row would be
  absent. `require_postgres_url()` raises (never skips), preserving the no-skip policy. This
  test EXECUTES in the final clean-room PG gate (Task 10), not in the per-task SQLite run.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/connectors/runs/executor.py tests/connectors/runs/test_executor.py tests/connectors/runs/test_executor_rls_postgres.py
  git commit -m "feat(connectors): ConnectorJobExecutor worker + in-process dup registry"
  ```

---

### Task 3: Repository readers (cheap, no network)

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/credentials.py`
  - add `get_credential(...)` to `SqlAlchemyConnectorCredentialRepository` after `_to_entry`
    (current `_to_entry` ends line 174)
- Modify: `backend/ums_smart_revenue/connectors/runs/repository.py`
  - add `find_active_runs_for_scope(...)` after `list_runs` (ends line 353)
- Test: extend `tests/api/test_connectors_api.py` for `get_credential` (it already seeds
  `ApiConnectorCredentialORM` via `SecurityBase`) and
  `tests/connectors/runs/test_runs_repository.py` (existing repository test module) for
  `find_active_runs_for_scope`

Confirmed: `finish_run` (`repository.py:187-215`) already performs the RUNNING -> FAILED
transition (requires status `RUNNING`, `for_update` lock, `flush` only — no commit), so the
orphan-supersede in Task 4 reuses `finish_run`; **no new supersede method is needed.**

Steps:

- [ ] Write the failing tests. First, in `tests/connectors/runs/test_runs_repository.py` add:

```python
def test_find_active_runs_for_scope_returns_only_running_matching_scope(
    sqlite_session,
):
    """Only RUNNING rows for the exact scope are returned, newest started first."""
    from datetime import UTC, datetime, timedelta
    from uuid import UUID, uuid4

    from ums_smart_revenue.connectors.runs.repository import (
        find_active_runs_for_scope,
        start_run,
    )
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    tenant = UUID(UMS_TENANT_ID)
    # Two RUNNING rows for the target scope at different started_at.
    older = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant,
        connector_key="youtube_reporting",
        account_id="acct-1",
        report_month="2026-03",
        triggered_by_user_id=None,
        started_at=datetime.now(UTC) - timedelta(hours=10),
        status="RUNNING",
        counts_json={},
        error_summary=None,
    )
    newer = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant,
        connector_key="youtube_reporting",
        account_id="acct-1",
        report_month="2026-03",
        triggered_by_user_id=None,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        status="RUNNING",
        counts_json={},
        error_summary=None,
    )
    # A terminal row (same scope) and a RUNNING row for another scope.
    terminal = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant,
        connector_key="youtube_reporting",
        account_id="acct-1",
        report_month="2026-03",
        triggered_by_user_id=None,
        started_at=datetime.now(UTC) - timedelta(hours=2),
        finished_at=datetime.now(UTC) - timedelta(hours=2),
        status="SUCCEEDED",
        counts_json={},
        error_summary=None,
    )
    other = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant,
        connector_key="adsense",
        account_id="acct-1",
        report_month="2026-03",
        triggered_by_user_id=None,
        started_at=datetime.now(UTC),
        status="RUNNING",
        counts_json={},
        error_summary=None,
    )
    sqlite_session.add_all([older, newer, terminal, other])
    sqlite_session.flush()

    rows = find_active_runs_for_scope(
        sqlite_session,
        tenant_id=tenant,
        connector_key="youtube_reporting",
        account_id="acct-1",
        report_month="2026-03",
    )
    assert [r.status for r in rows] == ["RUNNING", "RUNNING"]
    # newest started_at first.
    assert rows[0].started_at >= rows[1].started_at
    assert {r.connector_key for r in rows} == {"youtube_reporting"}
```

  (If `tests/connectors/runs/test_runs_repository.py` has no `sqlite_session` fixture, mirror
  the in-memory session setup the module already uses for `start_run`/`finish_run`; the
  failing-first run will reveal the fixture name.)

  Then, in `tests/api/test_connectors_api.py` add a credentials-reader test:

```python
def test_get_credential_found_none_and_wrong_tenant(tmp_path):
    """get_credential returns the entry for the scope, None when absent/cross-tenant."""
    from ums_smart_revenue.connectors.credentials import (
        SqlAlchemyConnectorCredentialRepository,
    )

    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    other_tenant = UUID("00000000-0000-0000-0000-0000000000ff")
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=UUID(UMS_TENANT_ID),
                connector_key="youtube_reporting",
                account_id="acct-1",
                encrypted_secret_ref="secret-manager://ums/yt/acct-1",
                status="active",
            )
        )
        session.commit()
    with Session(engine) as session:
        repo = SqlAlchemyConnectorCredentialRepository(
            session, tenant_id=UMS_TENANT_ID
        )
        found = repo.get_credential(
            session,
            tenant_id=UUID(UMS_TENANT_ID),
            connector_key="youtube_reporting",
            account_id="acct-1",
        )
        assert found is not None
        assert found.status == "active"
        assert repo.get_credential(
            session,
            tenant_id=UUID(UMS_TENANT_ID),
            connector_key="youtube_reporting",
            account_id="missing",
        ) is None
        assert repo.get_credential(
            session,
            tenant_id=other_tenant,
            connector_key="youtube_reporting",
            account_id="acct-1",
        ) is None
    engine.dispose()
```

- [ ] Run to fail:
  `python -m pytest tests/connectors/runs/test_runs_repository.py -q tests/api/test_connectors_api.py::test_get_credential_found_none_and_wrong_tenant -q`
  Expected: `ImportError: cannot import name 'find_active_runs_for_scope'` and
  `AttributeError: 'SqlAlchemyConnectorCredentialRepository' object has no attribute
  'get_credential'`.

- [ ] Minimal implementation (a). In `backend/ums_smart_revenue/connectors/credentials.py`,
  add a method to `SqlAlchemyConnectorCredentialRepository` after `_to_entry` (after line
  174):

```python
    # ========================================================================
    # Purpose: Tenant-scoped point read of one connector credential row for the
    #   pre-submission validation in POST /connectors/jobs (cheap; NO OAuth
    #   refresh). Returns the serialized entry (status visible) or None.
    # Database/ORM: ApiConnectorCredentialORM (read only).
    # Standards: tenant_id always filtered; reuses _to_entry so the read shape
    #   stays single-sourced.
    # Blast Radius: Connector credential read surface only. No finance, audit,
    #   auth-mutation, or graph projection impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py ->
    #     request_connector_job validates credential existence/status.
    # ========================================================================
    def get_credential(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
    ) -> ConnectorCredentialEntry | None:
        """Return the tenant-scoped credential entry for the scope, or None."""
        row = session.scalars(
            select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.tenant_id == tenant_id,
                ApiConnectorCredentialORM.connector_key == connector_key,
                ApiConnectorCredentialORM.account_id == account_id,
            )
        ).one_or_none()
        if row is None:
            return None
        return self._to_entry(row)
```

  Minimal implementation (b). In `backend/ums_smart_revenue/connectors/runs/repository.py`,
  add after `list_runs` (after line 353):

```python
# ============================================================================
# Purpose: Return the tenant-scoped RUNNING connector runs for one exact scope
#   (tenant, connector_key, account_id, report_month), newest started_at first.
#   Powers the POST /connectors/jobs secondary duplicate guard + orphan-
#   supersede decision (a stale RUNNING row from a dead process).
# Database/ORM: ConnectorRunORM (read only).
# Standards: tenant filter + status='RUNNING' + the full scope tuple always
#   applied; ordered started_at desc so the youngest RUNNING row is first.
# Blast Radius: Connector run read surface only. No finance, auth, audit, or
#   graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py ->
#     request_connector_job duplicate/orphan-supersede logic.
#   - Function: finish_run -> reused to rewrite a superseded orphan to FAILED.
# ============================================================================
def find_active_runs_for_scope(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
) -> list[ConnectorRunEntry]:
    """List RUNNING runs for one exact scope, newest started_at first."""
    stmt = (
        select(ConnectorRunORM)
        .where(
            ConnectorRunORM.tenant_id == tenant_id,
            ConnectorRunORM.connector_key == connector_key,
            ConnectorRunORM.account_id == account_id,
            ConnectorRunORM.report_month == report_month,
            ConnectorRunORM.status == "RUNNING",
        )
        .order_by(ConnectorRunORM.started_at.desc(), ConnectorRunORM.id.desc())
    )
    return [_to_entry(row) for row in session.scalars(stmt).all()]
```

- [ ] Run to pass:
  `python -m pytest tests/connectors/runs/test_runs_repository.py -q tests/api/test_connectors_api.py::test_get_credential_found_none_and_wrong_tenant -q`
  Expected: both new tests pass.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/connectors/credentials.py backend/ums_smart_revenue/connectors/runs/repository.py tests/connectors/runs/test_runs_repository.py tests/api/test_connectors_api.py
  git commit -m "feat(connectors): get_credential + find_active_runs_for_scope readers"
  ```

---

### Task 4: Route upgrade — `POST /connectors/jobs` executes

**Files:**
- Modify: `backend/ums_smart_revenue/api/connectors.py`
  - imports block (lines 31-43): add `Request` to the fastapi import (line 7); add
    `run_one` to the `connectors.runs.orchestrator` import (line 37); add
    `find_active_runs_for_scope`, `finish_run`, `validate_report_month` to the
    `connectors.runs.repository` import (lines 38-42); add
    `from ums_smart_revenue.connectors.google.registry import known_keys`; add
    `from ums_smart_revenue.connectors.runs.executor import ConnectorJobActor`;
    add `from ums_smart_revenue.db.security_models import UserORM`; import `select` from
    sqlalchemy for the users-existence check
  - `ConnectorJobRequest` (lines 72-77): add `report_month` + `dry_run`
  - `request_connector_job` (lines 261-284): full rewrite to the control flow
- Test: `tests/api/test_connectors_api.py` — rewrite the pinning test
  `test_revenue_operations_admin_can_request_connector_job_and_audit` (lines 252-279) and add
  new cases

Steps:

- [ ] Write the failing tests. Replace the pinning test
  (`tests/api/test_connectors_api.py:252-279`) with the new contract and add cases. First add
  a fake executor + a seed helper near the top of the test module:

```python
class _FakeExecutor:
    """Records submit() calls and answers has_active_job() from a flag set."""

    def __init__(self, *, active=False):
        self.active = active
        self.submit_calls: list[dict] = []

    def has_active_job(self, **kwargs) -> bool:
        return self.active

    def submit(self, **kwargs):
        self.submit_calls.append(kwargs)
        return None


def _enable_executor_app(database_url: str, executor):
    """Build an app with the executor enabled + a fake executor on app.state."""
    import os

    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    from ums_smart_revenue.config.settings import load_app_settings

    load_app_settings.cache_clear()
    app = create_app(database_url=database_url)
    app.state.connector_job_executor = executor
    return app


def _seed_active_credential(database_url: str, *, status="active"):
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=UUID(UMS_TENANT_ID),
                connector_key="youtube_reporting",
                account_id="content-owner-1",
                encrypted_secret_ref="secret-manager://ums/yt/content-owner-1",
                status=status,
            )
        )
        session.commit()
    engine.dispose()
```

  New pinning test (replaces the old one):

```python
def test_revenue_operations_admin_can_request_connector_job_and_audit(tmp_path):
    """revenue_operations_admin submits a job: 202 submitted + one job_submitted audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    fake = _FakeExecutor(active=False)
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Manual retry after report availability delay",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()

    assert response.status_code == 202
    body = response.json()
    assert body["connector_key"] == "youtube_reporting"
    assert body["execution_status"] == "submitted"
    assert body["report_month"] == "2026-03"
    assert body["dry_run"] is False
    assert audit_log.event_type == "CONNECTOR_JOB_RUN"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "youtube_reporting"
    assert audit_log.details["action"] == "job_submitted"
    assert len(fake.submit_calls) == 1
    call = fake.submit_calls[0]
    assert call["connector_key"] == "youtube_reporting"
    assert call["account_id"] == "content-owner-1"
    assert call["report_month"] == "2026-03"
    assert call["dry_run"] is False
    assert isinstance(call["actor_identity"], ConnectorJobActor)


def test_request_connector_job_missing_permission_403(tmp_path):
    """assistant_analyst is denied with the run_jobs permission detail (no audit)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    fake = _FakeExecutor()
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers("assistant_analyst"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Should be denied",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.run_jobs"
    assert fake.submit_calls == []


def test_request_connector_job_503_when_executor_disabled(tmp_path):
    """Executor disabled -> 503 + a job_rejected/executor_disabled audit."""
    import os

    os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
    from ums_smart_revenue.config.settings import load_app_settings

    load_app_settings.cache_clear()
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Try while disabled",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 503
    assert audit_log.details["action"] == "job_rejected"
    assert audit_log.details["rejection"] == "executor_disabled"


def test_request_connector_job_422_unknown_connector(tmp_path):
    """Unknown connector key -> 422 + job_rejected/unknown_connector."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    fake = _FakeExecutor()
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers("revenue_operations_admin", "connector", "made_up"),
        json={
            "connector_key": "made_up",
            "account_id": "acct-1",
            "report_month": "2026-03",
            "reason": "Unknown key",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 422
    assert audit_log.details["rejection"] == "unknown_connector"
    assert fake.submit_calls == []


def test_request_connector_job_422_bad_month(tmp_path):
    """Malformed report_month -> 422 + job_rejected/invalid_month."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    fake = _FakeExecutor()
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-13",
            "reason": "Bad month",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 422
    assert audit_log.details["rejection"] == "invalid_month"
    assert fake.submit_calls == []


def test_request_connector_job_422_missing_credential(tmp_path):
    """Missing credential -> 422 + job_rejected/credential_not_found."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    fake = _FakeExecutor()
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "No credential exists",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 422
    assert audit_log.details["rejection"] == "credential_not_found"
    assert fake.submit_calls == []


def test_request_connector_job_422_inactive_credential(tmp_path):
    """Inactive credential -> 422 + job_rejected/credential_inactive."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url, status="disabled")
    fake = _FakeExecutor()
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Inactive credential",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 422
    assert audit_log.details["rejection"] == "credential_inactive"
    assert fake.submit_calls == []


def test_request_connector_job_409_duplicate_in_flight(tmp_path):
    """has_active_job True -> 409 + job_rejected/duplicate_in_flight."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    fake = _FakeExecutor(active=True)
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Duplicate",
        },
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert response.status_code == 409
    assert audit_log.details["rejection"] == "duplicate_in_flight"
    assert fake.submit_calls == []


def test_request_connector_job_orphan_supersede_then_accept(tmp_path):
    """A stale RUNNING row older than the threshold is flipped FAILED + 202."""
    from datetime import UTC, datetime, timedelta

    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_active_credential(database_url)
    # Seed a RUNNING row older than the default 6h threshold.
    seed_connector_run(
        database_url,
        connector_key="youtube_reporting",
        account_id="content-owner-1",
        status="RUNNING",
        error_summary=None,
        started_at=datetime.now(UTC) - timedelta(hours=12),
        finished_at=None,
        report_month="2026-03",
    )
    fake = _FakeExecutor(active=False)
    client = TestClient(_enable_executor_app(database_url, fake))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-03",
            "reason": "Supersede stale run",
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["execution_status"] == "submitted"
    engine = create_engine(database_url)
    with Session(engine) as session:
        run = session.scalars(
            select(ConnectorRunORM).where(
                ConnectorRunORM.connector_key == "youtube_reporting",
                ConnectorRunORM.account_id == "content-owner-1",
            )
        ).one()
        audit_log = session.scalars(select(AuditLogORM)).one()
    engine.dispose()
    assert run.status == "FAILED"
    assert "superseded" in (run.error_summary or "")
    assert audit_log.details["action"] == "job_submitted"
    assert audit_log.details["superseded_run_id"] == str(run.id)
    assert len(fake.submit_calls) == 1
```

  Note: `seed_connector_run` (already at `tests/api/test_connectors_api.py:510`) accepts a
  `report_month` kwarg and seeds for `UMS_TENANT_ID`, which matches the request's
  `_resolve_tenant_uuid`. The 409/orphan tests rely on `has_active_job=False` (the in-process
  registry is empty in a fresh fake) so the secondary DB reader is exercised.

- [ ] Run to fail:
  `python -m pytest tests/api/test_connectors_api.py -q -k connector_job`
  Expected: the rewritten pinning test + new tests fail (the route still returns
  `recorded_not_executed`, has no `report_month` field, never reads `app.state`).

- [ ] Minimal implementation. Update the imports in
  `backend/ums_smart_revenue/api/connectors.py`. Line 7 becomes:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
```

  Add `select` to the sqlalchemy import group (a new line near line 10):

```python
from sqlalchemy import select
```

  Extend the repository imports (lines 38-42) and add registry/executor/ORM/settings. The
  route does NOT import `run_one` (the executor calls it) and does NOT use
  `resolve_connector_credentials` (it uses the cheap `get_credential` reader, no OAuth refresh
  pre-submission) — so the existing line-37 `resolve_connector_credentials` import stays only
  if the test-connection route still needs it (it does: `connectors.py:326`), leave it.
  Add these imports:

```python
from ums_smart_revenue.connectors.google.registry import known_keys
from ums_smart_revenue.connectors.runs.executor import ConnectorJobActor
from ums_smart_revenue.connectors.runs.repository import (
    MAX_CONNECTOR_RUN_PAGE_SIZE,
    ConnectorRunValidationError,
    find_active_runs_for_scope,
    finish_run,
    list_runs,
    validate_report_month,
)
from ums_smart_revenue.db.security_models import UserORM
from ums_smart_revenue.config.settings import load_app_settings
```

  Do NOT add `run_one` to `connectors.py` — the route never calls it (ruff would flag it
  unused). `resolve_connector_credentials` keeps its existing line-37 import (the
  test-connection route at `connectors.py:326` still uses it); the jobs route does not.

  Extend `ConnectorJobRequest` (lines 72-77):

```python
class ConnectorJobRequest(NonBlankRequestModel):
    """Request body for requesting a connector data-ingest job."""

    connector_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    report_month: str = Field(min_length=1)
    dry_run: bool = False
    reason: str = Field(min_length=1)
```

  Replace `request_connector_job` (lines 261-284) with the executing handler:

```python
# ============================================================================
# Purpose: Submit a real Google ingest pull to the module-owned
#   ConnectorJobExecutor and return 202 "submitted" immediately. Cheap
#   in-request validation only (no OAuth refresh, no run_one inline). Control
#   flow order is load-bearing and fail-closed: authz 403 -> 503 disabled ->
#   422 unknown-key -> 422 bad-month -> 422 missing/inactive cred -> 409 dup /
#   orphan-supersede -> 202 submit. Exactly ONE route-owned audit row
#   (job_submitted / job_rejected); run_one emits its own STARTED/FINISHED
#   edges later on the worker session.
# Database/ORM: ApiConnectorCredentialORM + ConnectorRunORM (reads via
#   get_credential / find_active_runs_for_scope; finish_run rewrites a
#   superseded orphan), AuditLogORM (one row), UserORM (existence probe).
# Standards: RUN_CONNECTOR_JOBS@connector gate first (HTTPException 403, no
#   audit). All non-2xx-but-audited responses use JSONResponse (not
#   HTTPException) so the request session commits the audit row (mirrors the
#   test-route 404 pattern). Audit details carry only machine tokens, never
#   str(exc). triggered_by_user_id degrades to None unless a users row exists.
# Blast Radius: Authorization (unchanged gate), audit (additive details
#   actions), connector run lifecycle (orphan supersede via finish_run on the
#   tenant lane). No finance math change; no graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> submit().
#   - File: backend/ums_smart_revenue/app.py -> app.state.connector_job_executor.
#   - File: Docs/12_BACKEND_API_SPEC.md -> POST /connectors/jobs contract.
# ============================================================================
@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def request_connector_job(
    payload: ConnectorJobRequest,
    request: Request,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Validate cheaply, submit the pull to the executor, and return 202 submitted."""
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_connector_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)

    tenant_id = _resolve_tenant_uuid(user)
    executor = getattr(request.app.state, "connector_job_executor", None)
    if executor is None:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            rejection="executor_disabled",
            detail="Connector job executor is disabled",
        )

    if payload.connector_key not in known_keys():
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="unknown_connector",
            detail="Unknown connector key",
        )

    try:
        validate_report_month(payload.report_month)
    except ConnectorRunValidationError:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="invalid_month",
            detail="report_month must use YYYY-MM",
        )

    repository = SqlAlchemyConnectorCredentialRepository(session, tenant_id=tenant_id)
    credential = repository.get_credential(
        session,
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
    )
    if credential is None:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="credential_not_found",
            detail="Connector credential not found",
        )
    if credential.status != "active":
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="credential_inactive",
            detail="Connector credential is not active",
        )

    if executor.has_active_job(
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        report_month=payload.report_month,
    ):
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_409_CONFLICT,
            rejection="duplicate_in_flight",
            detail="A connector job for this scope is already in flight",
        )

    superseded_run_id = _supersede_or_block_running_runs(
        session,
        tenant_id=tenant_id,
        payload=payload,
        stale_hours=load_app_settings().connector_job_stale_running_hours,
    )
    if isinstance(superseded_run_id, _DuplicateRunBlock):
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_409_CONFLICT,
            rejection="duplicate_in_flight",
            detail="A connector job for this scope is already in flight",
        )

    triggered_by = _resolve_triggered_by_user_id(session, user, tenant_id)
    details: dict[str, object] = {
        "action": "job_submitted",
        "report_month": payload.report_month,
        "dry_run": payload.dry_run,
    }
    if superseded_run_id is not None:
        details["superseded_run_id"] = superseded_run_id
    record = _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details=details,
    )
    executor.submit(
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        report_month=payload.report_month,
        dry_run=payload.dry_run,
        triggered_by_user_id=triggered_by,
        actor_identity=ConnectorJobActor(
            user_id=user.user_id, email=user.email, role=str(user.role_assignments)
        ),
    )
    return {
        "connector_key": payload.connector_key,
        "account_id": payload.account_id,
        "report_month": payload.report_month,
        "dry_run": payload.dry_run,
        "execution_status": "submitted",
        "audit_event": audit_record_to_api(record),
    }
```

  Add the helper functions and the sentinel near `_audit_connector_change` (after line 476):

```python
class _DuplicateRunBlock:
    """Sentinel: a fresh RUNNING row exists for the scope -> reject as duplicate."""


# ============================================================================
# Purpose: Secondary cross-process duplicate guard. Inspect RUNNING
#   connector_runs for the exact scope; the youngest younger than stale_hours
#   blocks (returns _DuplicateRunBlock); an older row is an orphan from a dead
#   process -> rewrite it FAILED via finish_run (tenant-lane; connector_runs is
#   tenant-writable) and return its id so the route records superseded_run_id.
# Database/ORM: ConnectorRunORM (read via find_active_runs_for_scope; FAILED
#   rewrite via finish_run, which requires RUNNING + flush-only).
# Standards: TOCTOU is code-level only (the index is non-unique); accepted at
#   max_workers=1; partial-unique-index deferred (documented in the spec).
# Blast Radius: Connector run lifecycle only. No finance, audit (the route
#   owns its row), or graph projection impact.
# Connections:
#   - Function: find_active_runs_for_scope -> the RUNNING reader.
#   - Function: finish_run -> the RUNNING->FAILED transition.
# ============================================================================
def _supersede_or_block_running_runs(
    session: Session,
    *,
    tenant_id: UUID,
    payload: ConnectorJobRequest,
    stale_hours: int,
) -> str | _DuplicateRunBlock | None:
    """Block on a fresh RUNNING run; supersede a stale one and return its id."""
    running = find_active_runs_for_scope(
        session,
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        report_month=payload.report_month,
    )
    if not running:
        return None
    threshold = datetime.now(UTC) - timedelta(hours=stale_hours)
    youngest = running[0]
    if youngest.started_at >= threshold:
        return _DuplicateRunBlock()
    superseded: str | None = None
    for entry in running:
        if entry.started_at < threshold:
            finish_run(
                session,
                tenant_id=tenant_id,
                connector_run_id=UUID(entry.id),
                status="FAILED",
                counts=dict(entry.counts),
                error_summary="orphaned RUNNING run superseded by new job",
            )
            superseded = entry.id
    return superseded


def _resolve_triggered_by_user_id(
    session: Session, user: UserPrincipal, tenant_id: UUID
) -> UUID | None:
    """Return the principal UUID only if it is a real users row for the tenant."""
    try:
        candidate = UUID(user.user_id)
    except (ValueError, TypeError):
        return None
    exists = session.scalar(
        select(UserORM.id).where(
            UserORM.id == candidate, UserORM.tenant_id == tenant_id
        )
    )
    return candidate if exists is not None else None


# ============================================================================
# Purpose: Write the single route-owned CONNECTOR_JOB_RUN job_rejected audit
#   row, then return a JSONResponse with the given status so the request
#   session COMMITS the audit row (HTTPException would roll it back). Audit
#   details carry only machine tokens.
# Database/ORM: AuditLogORM (one row via the request-scoped sink).
# Standards: JSONResponse-commits-the-audit pattern (mirrors the test-route
#   404 at connectors.py:370-380). No str(exc) interpolation.
# Blast Radius: Audit (additive job_rejected action). No finance, auth, or
#   graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> request_connector_job.
# ============================================================================
def _reject_connector_job(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
    status_code: int,
    rejection: str,
    detail: str,
) -> JSONResponse:
    """Audit a rejected job submission and return a committing JSONResponse."""
    _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details={
            "action": "job_rejected",
            "rejection": rejection,
            "report_month": payload.report_month,
        },
    )
    return JSONResponse(status_code=status_code, content={"detail": detail})
```

  Add the time imports near line 3 (`from datetime import datetime`): change to

```python
from datetime import UTC, datetime, timedelta
```

- [ ] Run to pass:
  `python -m pytest tests/api/test_connectors_api.py -q`
  Expected: the rewritten pinning test + the eight new `connector_job` tests pass, and the
  existing credential/test-connection/run-history tests stay green.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/api/connectors.py tests/api/test_connectors_api.py
  git commit -m "feat(api): POST /connectors/jobs submits to executor (202 submitted)"
  ```

---

### Task 5: App wiring — construct + close the executor

**Files:**
- Modify: `backend/ums_smart_revenue/app.py`
  - import `asynccontextmanager` + the executor + settings
  - construct the executor inside `if resolved_database_url:` (after line 110) when enabled
  - introduce a FastAPI `lifespan` passed to `FastAPI(...)` (line 99-103) whose shutdown
    closes the executor
- Test: `tests/api/test_app_connector_executor.py` (new)

Steps:

- [ ] Write the failing test. Create `tests/api/test_app_connector_executor.py`:

```python
"""Tests for ConnectorJobExecutor wiring + lifespan teardown in create_app."""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.runs.executor import ConnectorJobExecutor


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'app.db').as_posix()}"


def test_executor_attached_when_enabled(tmp_path) -> None:
    """With the flag on, app.state carries a ConnectorJobExecutor."""
    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        executor = getattr(app.state, "connector_job_executor", None)
        assert isinstance(executor, ConnectorJobExecutor)
        executor.close()
    finally:
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()


def test_no_executor_when_disabled(tmp_path) -> None:
    """Default (disabled) leaves no executor attribute on app.state."""
    os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
    load_app_settings.cache_clear()
    app = create_app(database_url=_sqlite_url(tmp_path))
    assert getattr(app.state, "connector_job_executor", None) is None


def test_lifespan_shutdown_closes_executor(tmp_path) -> None:
    """Exiting the TestClient lifespan calls the executor's close()."""
    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        closed = {"count": 0}
        real = app.state.connector_job_executor

        def _spy() -> None:
            closed["count"] += 1
            real.close()

        app.state.connector_job_executor.close = _spy  # type: ignore[method-assign]
        with TestClient(app):
            pass
        assert closed["count"] == 1
    finally:
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()
```

- [ ] Run to fail:
  `python -m pytest tests/api/test_app_connector_executor.py -q`
  Expected: `test_executor_attached_when_enabled` fails (no `connector_job_executor`
  attribute), `test_lifespan_shutdown_closes_executor` fails (no lifespan close).

- [ ] Minimal implementation. In `backend/ums_smart_revenue/app.py`, add imports near the top
  (after line 2):

```python
from contextlib import asynccontextmanager
```

  Add to the settings import group (lines 61-65):

```python
from ums_smart_revenue.config.settings import (
    AUTHZ_SOURCE_DATABASE,
    AUTHZ_SOURCE_HEADERS,
    load_app_settings,
)
from ums_smart_revenue.connectors.runs.executor import ConnectorJobExecutor
```

  Introduce the lifespan and pass it to `FastAPI(...)`. Replace the `FastAPI(...)`
  construction (lines 99-103) with a lifespan-aware version:

```python
    # ========================================================================
    # Purpose: Close the module-owned ConnectorJobExecutor on app shutdown so
    #   worker threads tear down deterministically (the weakref.finalize GC
    #   backstop is a safety net, not the primary teardown). No startup work.
    # Database/ORM: None directly; the executor owns its own session factory.
    # Standards: getattr-guarded so a disabled app (no executor) shuts down
    #   cleanly. Fail-closed default OFF means the import-time app spawns no
    #   threads.
    # Blast Radius: Process lifecycle / threads only. No finance, auth, audit,
    #   or graph projection impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> close().
    # ========================================================================
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        """Yield through serving, then close the connector-job executor if present."""
        try:
            yield
        finally:
            executor = getattr(app.state, "connector_job_executor", None)
            if executor is not None:
                executor.close()

    _app = FastAPI(
        title="UMS Smart Revenue Control Center API",
        version="0.1.0",
        description="Numbers-first internal revenue control API for UMS.",
        lifespan=_lifespan,
    )
```

  Inside the `if resolved_database_url:` block, after `platform_session_factory` is built
  (after line 110), construct the executor when enabled:

```python
        if settings.connector_job_executor_enabled:
            _app.state.connector_job_executor = ConnectorJobExecutor(
                session_factory=build_session_factory(resolved_database_url),
                max_workers=settings.connector_job_max_workers,
                stale_running_hours=settings.connector_job_stale_running_hours,
            )
```

- [ ] Run to pass:
  `python -m pytest tests/api/test_app_connector_executor.py -q`
  Expected: all three wiring tests pass.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/app.py tests/api/test_app_connector_executor.py
  git commit -m "feat(app): wire ConnectorJobExecutor on app.state + lifespan shutdown"
  ```

---

### Task 6: Part 2 — migration + ORM columns

**Files:**
- Create:
  `backend/ums_smart_revenue/db/alembic/versions/20260612_0001_connector_credential_refresh_telemetry.py`
  (`revision="20260612_0001"`, `down_revision="20260609_0002"` — verified the single linear
  head: no version file has `20260609_0002` as its `down_revision`)
- Modify: `backend/ums_smart_revenue/db/security_models.py`
  - `ApiConnectorCredentialORM` (lines 406-463): four `mapped_column`s + a `CheckConstraint`
- Test: `tests/db/test_connector_credential_refresh_telemetry_migration_postgres.py` (new;
  copies `tests/db/test_raw_report_files_purge_migration.py`)

Steps:

- [ ] Write the failing test. Create
  `tests/db/test_connector_credential_refresh_telemetry_migration_postgres.py`:

```python
"""PostgreSQL round-trip tests for Part 2 connector credential telemetry columns."""

from pathlib import Path
from uuid import uuid4

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url  # sibling via pytest prepend
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIOR_HEAD = "20260609_0002"
TELEMETRY_HEAD = "20260612_0001"
_NEW_COLUMNS = {
    "last_refresh_attempt_at",
    "token_expiry_at",
    "last_refresh_status",
    "last_refresh_error_class",
}


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str) -> object:
    reset_public_schema(postgres_url)
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_tenant(conn, tenant_id, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (:id, :slug, :display_name, 'USD', 'ACTIVE')"
        ),
        {"id": tenant_id, "slug": slug, "display_name": slug.title()},
    )


def _insert_credential(conn, tenant_id, credential_id) -> None:
    conn.execute(
        text(
            "INSERT INTO api_connector_credentials "
            "(id, tenant_id, connector_key, account_id, encrypted_secret_ref, status) "
            "VALUES (:id, :tenant_id, 'youtube_reporting', 'acct-1', "
            "'secret-manager://ums/yt/acct-1', 'active')"
        ),
        {"id": credential_id, "tenant_id": tenant_id},
    )


def test_upgrade_adds_telemetry_columns(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.issubset(columns)


def test_downgrade_then_upgrade_round_trip(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    command.downgrade(alembic_config, PRIOR_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.isdisjoint(columns)
    # Leave the DB at head for downstream PG-tier tests.
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    inspector = inspect(fresh_engine)
    columns = {c["name"] for c in inspector.get_columns("api_connector_credentials")}
    assert _NEW_COLUMNS.issubset(columns)


def test_last_refresh_status_check_positive_and_negative(
    alembic_config: Config, fresh_engine: object
) -> None:
    command.upgrade(alembic_config, TELEMETRY_HEAD)
    tenant_id = uuid4()
    credential_id = uuid4()
    with fresh_engine.begin() as conn:
        _insert_tenant(conn, tenant_id, "tenant-a")
        _insert_credential(conn, tenant_id, credential_id)
        conn.execute(
            text(
                "UPDATE api_connector_credentials SET last_refresh_status = 'failed' "
                "WHERE id = :id"
            ),
            {"id": credential_id},
        )
        value = conn.execute(
            text(
                "SELECT last_refresh_status FROM api_connector_credentials "
                "WHERE id = :id"
            ),
            {"id": credential_id},
        ).scalar()
    assert value == "failed"
    with pytest.raises(DatabaseError):
        with fresh_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE api_connector_credentials "
                    "SET last_refresh_status = 'bogus' WHERE id = :id"
                ),
                {"id": credential_id},
            )
```

- [ ] Run to fail (requires the disposable PG container; `require_postgres_url` raises if
  `UMS_TEST_DATABASE_URL` is unset):
  `python -m pytest tests/db/test_connector_credential_refresh_telemetry_migration_postgres.py -q`
  Expected: failure at `command.upgrade(... TELEMETRY_HEAD)` — `KeyError`/
  `revision '20260612_0001' not found` (the migration does not exist yet).

- [ ] Minimal implementation (migration). Create
  `backend/ums_smart_revenue/db/alembic/versions/20260612_0001_connector_credential_refresh_telemetry.py`:

```python
"""Add credential refresh telemetry columns to api_connector_credentials.

Revision ID: 20260612_0001
Revises: 20260609_0002
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op

# ============================================================================
# Purpose: Additive Part 2 telemetry columns on api_connector_credentials so
#   credential refresh outcome (attempt time, status, exception class name,
#   token expiry) persists. Four NULLABLE columns + a CHECK on
#   last_refresh_status (IS NULL escape so existing rows pass). No backfill.
# Database/ORM: api_connector_credentials (tenant-scoped, tenant-writable; NOT
#   in TENANT_PLATFORM_ONLY_WRITE_TABLES, so no grant-pin impact).
# Standards: batch_alter_table so the SQLite test tier round-trips; CHECK name
#   ck_connector_last_refresh_status MUST match the ORM + tests. downgrade
#   drops the constraint before the columns. error_class stores the class name
#   only, never message text.
# Blast Radius: Connector credential read surface (new fields). No finance,
#   auth, audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py ->
#     ApiConnectorCredentialORM mirrors these columns + CHECK.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     resolve_connector_credentials stamps these columns.
# ============================================================================

revision = "20260612_0001"
down_revision = "20260609_0002"
branch_labels = None
depends_on = None

_STATUS_CHECK = (
    "last_refresh_status IS NULL "
    "OR last_refresh_status IN ('succeeded', 'failed')"
)


def upgrade() -> None:
    """Add the four nullable telemetry columns + the status CHECK."""
    with op.batch_alter_table("api_connector_credentials") as batch:
        batch.add_column(
            sa.Column(
                "last_refresh_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "token_expiry_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch.add_column(
            sa.Column("last_refresh_status", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("last_refresh_error_class", sa.Text(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_connector_last_refresh_status", _STATUS_CHECK
        )


def downgrade() -> None:
    """Drop the status CHECK then the four telemetry columns."""
    with op.batch_alter_table("api_connector_credentials") as batch:
        batch.drop_constraint(
            "ck_connector_last_refresh_status", type_="check"
        )
        batch.drop_column("last_refresh_error_class")
        batch.drop_column("last_refresh_status")
        batch.drop_column("token_expiry_at")
        batch.drop_column("last_refresh_attempt_at")
```

  Minimal implementation (ORM). In `backend/ums_smart_revenue/db/security_models.py`, add the
  four columns to `ApiConnectorCredentialORM` after `tenant_id` (after line 436, before
  `__table_args__`):

```python
    last_refresh_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_expiry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refresh_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_refresh_error_class: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
```

  Add the CHECK to `__table_args__` (after the existing `ck_connector_status` CheckConstraint
  at lines 439-442):

```python
        CheckConstraint(
            "last_refresh_status IS NULL "
            "OR last_refresh_status IN ('succeeded', 'failed')",
            name="ck_connector_last_refresh_status",
        ),
```

  (`Text`, `CheckConstraint`, `DateTime`, `mapped_column` are already imported in this module
  — no new imports.)

- [ ] Run to pass (PG container up, `UMS_TEST_DATABASE_URL` set):
  `python -m pytest tests/db/test_connector_credential_refresh_telemetry_migration_postgres.py -q`
  Expected: the three migration tests pass; DB left at head `20260612_0001`.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/db/alembic/versions/20260612_0001_connector_credential_refresh_telemetry.py backend/ums_smart_revenue/db/security_models.py tests/db/test_connector_credential_refresh_telemetry_migration_postgres.py
  git commit -m "feat(db): credential refresh telemetry columns + CHECK migration"
  ```

---

### Task 7: Part 2 — serialization

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/credentials.py`
  - `ConnectorCredentialEntry` dataclass (lines 27-33): four new fields
  - `to_api()` (lines 35-42): four keys, datetimes `.isoformat()` None-guarded
  - `_to_entry` (lines 166-174): read the four ORM columns
  - import `datetime` for the field types
- Test: extend `tests/api/test_connectors_api.py`

Steps:

- [ ] Write the failing tests. Add to `tests/api/test_connectors_api.py`:

```python
def test_to_entry_maps_refresh_telemetry_columns(tmp_path):
    """_to_entry reads the four telemetry columns into the entry + to_api."""
    from datetime import UTC, datetime

    from ums_smart_revenue.connectors.credentials import (
        SqlAlchemyConnectorCredentialRepository,
    )

    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    stamped = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=UUID(UMS_TENANT_ID),
                connector_key="youtube_reporting",
                account_id="acct-1",
                encrypted_secret_ref="secret-manager://ums/yt/acct-1",
                status="active",
                last_refresh_attempt_at=stamped,
                token_expiry_at=stamped,
                last_refresh_status="succeeded",
                last_refresh_error_class=None,
            )
        )
        session.commit()
    with Session(engine) as session:
        repo = SqlAlchemyConnectorCredentialRepository(
            session, tenant_id=UMS_TENANT_ID
        )
        entry = repo.get_credential(
            session,
            tenant_id=UUID(UMS_TENANT_ID),
            connector_key="youtube_reporting",
            account_id="acct-1",
        )
    engine.dispose()
    assert entry is not None
    assert entry.last_refresh_status == "succeeded"
    api = entry.to_api()
    assert api["last_refresh_status"] == "succeeded"
    assert api["last_refresh_attempt_at"] == stamped.isoformat()
    assert api["token_expiry_at"] == stamped.isoformat()
    assert api["last_refresh_error_class"] is None


def test_to_api_serializes_none_telemetry(tmp_path):
    """to_api emits None for unstamped telemetry without raising."""
    from ums_smart_revenue.connectors.credentials import ConnectorCredentialEntry

    entry = ConnectorCredentialEntry(
        id="x",
        connector_key="youtube_reporting",
        account_id="acct-1",
        status="active",
        has_secret_ref=True,
        last_refresh_attempt_at=None,
        token_expiry_at=None,
        last_refresh_status=None,
        last_refresh_error_class=None,
    )
    api = entry.to_api()
    assert api["last_refresh_attempt_at"] is None
    assert api["token_expiry_at"] is None
    assert api["last_refresh_status"] is None


def test_list_credentials_api_includes_telemetry_fields(tmp_path):
    """GET /credentials surfaces the four telemetry keys (None when unstamped)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=UUID(UMS_TENANT_ID),
                connector_key="youtube_reporting",
                account_id="acct-1",
                encrypted_secret_ref="secret-manager://ums/yt/acct-1",
                status="active",
            )
        )
        session.commit()
    engine.dispose()
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/connectors/credentials", headers=auth_headers("connector_admin")
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "last_refresh_status" in item
    assert "last_refresh_attempt_at" in item
    assert "token_expiry_at" in item
    assert "last_refresh_error_class" in item
```

- [ ] Run to fail:
  `python -m pytest tests/api/test_connectors_api.py -q -k telemetry`
  Expected: `TypeError: ConnectorCredentialEntry.__init__() got an unexpected keyword
  argument 'last_refresh_attempt_at'` / missing `to_api` keys.

- [ ] Minimal implementation. In `backend/ums_smart_revenue/connectors/credentials.py`, add
  the datetime import (line 1 group):

```python
from datetime import datetime
```

  Extend the dataclass (lines 27-33):

```python
@dataclass(frozen=True)
class ConnectorCredentialEntry:
    id: str
    connector_key: str
    account_id: str
    status: str
    has_secret_ref: bool
    last_refresh_attempt_at: datetime | None = None
    token_expiry_at: datetime | None = None
    last_refresh_status: str | None = None
    last_refresh_error_class: str | None = None
```

  Extend `to_api()` (lines 35-42):

```python
    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "connector_key": self.connector_key,
            "account_id": self.account_id,
            "status": self.status,
            "has_secret_ref": self.has_secret_ref,
            "last_refresh_attempt_at": (
                self.last_refresh_attempt_at.isoformat()
                if self.last_refresh_attempt_at is not None
                else None
            ),
            "token_expiry_at": (
                self.token_expiry_at.isoformat()
                if self.token_expiry_at is not None
                else None
            ),
            "last_refresh_status": self.last_refresh_status,
            "last_refresh_error_class": self.last_refresh_error_class,
        }
```

  Extend `_to_entry` (lines 166-174):

```python
    @staticmethod
    def _to_entry(row: ApiConnectorCredentialORM) -> ConnectorCredentialEntry:
        return ConnectorCredentialEntry(
            id=str(row.id),
            connector_key=row.connector_key,
            account_id=row.account_id,
            status=row.status,
            has_secret_ref=bool(row.encrypted_secret_ref),
            last_refresh_attempt_at=row.last_refresh_attempt_at,
            token_expiry_at=row.token_expiry_at,
            last_refresh_status=row.last_refresh_status,
            last_refresh_error_class=row.last_refresh_error_class,
        )
```

- [ ] Run to pass:
  `python -m pytest tests/api/test_connectors_api.py -q`
  Expected: the three telemetry serialization tests pass; existing credential tests stay
  green.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/connectors/credentials.py tests/api/test_connectors_api.py
  git commit -m "feat(connectors): serialize credential refresh telemetry fields"
  ```

---

### Task 8: Part 2 — telemetry stamp at the refresh chokepoint

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/runs/orchestrator.py`
  - `resolve_connector_credentials` (verified lines **628-658** — the prompt's 609-643 is
    stale): add a private `_stamp_credential_refresh(...)` and wrap the
    `refresh_credentials(credentials)` call at line 657
- Test: `tests/connectors/runs/test_credential_refresh_telemetry.py` (new)

Steps:

- [ ] Write the failing test. Create
  `tests/connectors/runs/test_credential_refresh_telemetry.py`:

```python
"""Tests for the Part 2 credential refresh telemetry stamp in resolve_*."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.errors import OAuthRefreshError
from ums_smart_revenue.connectors.runs.orchestrator import (
    resolve_connector_credentials,
)
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    SecurityBase,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)


def _factory(tmp_path) -> sessionmaker:
    url = f"sqlite+pysqlite:///{(tmp_path / 'tel.db').as_posix()}"
    engine = create_engine(url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                encrypted_secret_ref="secret-manager://ums/yt/acct-1",
                status="active",
            )
        )
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fake_credentials(expiry):
    return SimpleNamespace(expiry=expiry)


def test_success_stamp_persists_after_caller_commit(tmp_path) -> None:
    """A successful refresh stamps succeeded + token_expiry and rides the commit."""
    from datetime import UTC, datetime

    factory = _factory(tmp_path)
    expiry = datetime(2026, 6, 1, tzinfo=UTC)
    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
        return_value={},
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
        return_value=_fake_credentials(expiry),
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ):
        with factory() as session:
            resolve_connector_credentials(
                session=session,
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
            )
            session.commit()
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status == "succeeded"
    assert row.last_refresh_error_class is None
    assert row.token_expiry_at == expiry
    assert row.last_refresh_attempt_at is not None


def test_failure_stamp_persists_and_reraises(tmp_path) -> None:
    """An OAuthRefreshError stamps failed (committed) AND still propagates."""
    factory = _factory(tmp_path)

    def _boom(_creds):
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
        return_value={},
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
        return_value=_fake_credentials(None),
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials",
        _boom,
    ):
        with factory() as session:
            with pytest.raises(OAuthRefreshError):
                resolve_connector_credentials(
                    session=session,
                    tenant_id=TENANT,
                    connector_key="youtube_reporting",
                    account_id="acct-1",
                )
            # The caller never commits on the failure path.
    # A separate session sees the committed failure stamp.
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status == "failed"
    assert row.last_refresh_error_class == "RuntimeError"
    assert row.token_expiry_at is None


def test_not_found_does_not_stamp(tmp_path) -> None:
    """CredentialNotFoundError (no refresh attempted) leaves telemetry NULL."""
    from ums_smart_revenue.connectors.google.errors import (
        CredentialNotFoundError,
    )

    factory = _factory(tmp_path)
    with factory() as session:
        with pytest.raises(CredentialNotFoundError):
            resolve_connector_credentials(
                session=session,
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="missing",
            )
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status is None
    assert row.last_refresh_attempt_at is None


def test_dry_run_success_not_persisted_without_caller_commit(tmp_path) -> None:
    """Success stamp rides the caller commit; no commit -> not persisted (dry-run)."""
    from datetime import UTC, datetime

    factory = _factory(tmp_path)
    expiry = datetime(2026, 6, 1, tzinfo=UTC)
    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
        return_value={},
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
        return_value=_fake_credentials(expiry),
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"
    ), patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ):
        with factory() as session:
            resolve_connector_credentials(
                session=session,
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
            )
            session.rollback()  # dry-run / CLI never commits the success stamp
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status is None
```

  (If `resolve_secret` / `build_credentials_from_payload` / `ensure_default_resolvers` are
  imported into `orchestrator` under different names, the failing-first run will show the real
  patch targets; adjust the patch paths to the names actually bound in `orchestrator.py`.)

- [ ] Run to fail:
  `python -m pytest tests/connectors/runs/test_credential_refresh_telemetry.py -q`
  Expected: `test_success_stamp_persists_after_caller_commit` and
  `test_failure_stamp_persists_and_reraises` fail (no stamp written;
  `last_refresh_status` is None / failure row not committed).

- [ ] Minimal implementation. In `backend/ums_smart_revenue/connectors/runs/orchestrator.py`,
  add the `datetime` import if not already present (`from datetime import UTC, datetime`), and
  rewrite the tail of `resolve_connector_credentials` (lines 651-658) plus add the private
  stamper. Replace lines 651-658 with:

```python
    ensure_default_resolvers()
    # FIX: Admin/API-created credentials may persist surrounding whitespace in
    # the secret URI. Normalize before resolver dispatch so valid refs do not
    # fail scheme lookup.
    payload = resolve_secret(credential.encrypted_secret_ref.strip())
    credentials = build_credentials_from_payload(payload)
    # ========================================================================
    # Purpose: Part 2 -- stamp credential refresh telemetry at the single
    #   chokepoint where the OAuth refresh outcome is known and the credential
    #   ORM row is in-session. SUCCESS rides the caller's commit (live run ->
    #   persisted at start_run commit; dry-run/CLI never commits -> not
    #   persisted, the intended dry-run semantics). FAILURE commits the stamp on
    #   THIS session (the only safe point: resolve runs BEFORE any run_one write,
    #   so nothing run-related is pending) then re-raises, leaving Bucket-A
    #   propagation intact (CLI exit 2 / test-route 200 / worker Bucket-A audit).
    # Database/ORM: ApiConnectorCredentialORM (UPDATE 4 telemetry columns;
    #   tenant-writable -> NO platform_lane needed).
    # Standards: error_class stores type(exc.inner or exc).__name__ only, never
    #   str(exc) (no message text). Invariant: resolve-runs-before-any-run-write
    #   -> the same-session failure commit is safe.
    # Blast Radius: Connector credential read surface. No finance, audit, or
    #   graph projection impact; OAuthRefreshError still propagates.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/oauth.py ->
    #     refresh_credentials populates credentials.expiry on success.
    # ========================================================================
    try:
        refresh_credentials(credentials)
    except OAuthRefreshError as exc:
        inner = getattr(exc, "inner", None)
        _stamp_credential_refresh(
            credential,
            status="failed",
            error_class=type(inner or exc).__name__,
            token_expiry=None,
        )
        session.commit()
        raise
    _stamp_credential_refresh(
        credential,
        status="succeeded",
        error_class=None,
        token_expiry=getattr(credentials, "expiry", None),
    )
    return credentials
```

  Add the private stamper right after `resolve_connector_credentials` (before the
  `_credentials_for_run` alias at line 662):

```python
def _stamp_credential_refresh(
    credential,
    *,
    status: str,
    error_class: str | None,
    token_expiry,
) -> None:
    """Mutate the in-session credential row's four telemetry columns."""
    credential.last_refresh_attempt_at = datetime.now(UTC)
    credential.last_refresh_status = status
    credential.last_refresh_error_class = error_class
    if token_expiry is not None:
        credential.token_expiry_at = token_expiry
```

  Ensure `OAuthRefreshError` is imported in `orchestrator.py` (it imports the error family;
  add `OAuthRefreshError` to that import group if absent).

- [ ] Run to pass:
  `python -m pytest tests/connectors/runs/test_credential_refresh_telemetry.py -q`
  Expected: all four telemetry tests pass. Also run
  `python -m pytest tests/connectors/runs -q` to confirm no orchestrator regressions.

- [ ] Commit:
  ```
  git add backend/ums_smart_revenue/connectors/runs/orchestrator.py tests/connectors/runs/test_credential_refresh_telemetry.py
  git commit -m "feat(connectors): stamp credential refresh telemetry in resolve_*"
  ```

---

### Task 9: Frontend — "Run pull" control

**Files:**
- Modify: `frontend/src/lib/api/types.ts`
  - `ConnectorJobRequestBody` (lines 513-519): `+report_month: string`, `+dry_run?: boolean`
  - `ConnectorJobResponse` (lines 521-529): doc the `'submitted'` status (type unchanged)
- Modify: `frontend/src/components/srcc/views/ConnectorsView.tsx`
  - `onRequestSync` (lines 164-177): add `report_month` (the month state) + `dry_run`, and a
    post-success runs reload (gated on a non-null result + `canViewConnectorHealth`)
  - `RequestJobSuccess` (lines 689-711): handle `execution_status==='submitted'` (the existing
    non-`recorded_not_executed` branch already renders green; extend its copy)
  - surface a `reload` out of `useRunHistoryFeedState` (line 514) by destructuring `reload`
    from `useConnectorRuns(...)` (line 520), resetting cursor/rows to page 1, and threading it
    `ConnectorsView -> ConnectorSidebar -> RunHistory -> RunHistoryFeed`
  - add an optional dry-run checkbox + reuse the existing month `<select>` over `MONTH_OPTIONS`
- Test: `frontend/src/components/srcc/views/__tests__/ConnectorsView.test.tsx`,
  `frontend/src/lib/api/__tests__/useConnectors.test.tsx`,
  `frontend/src/components/srcc/__tests__/AppShell.test.tsx`

Reuses the verified harness: `routeBoth`/`renderConnectorsView`/`runCalls`
(`ConnectorsView.test.tsx:162-206`), the request-job test (515-566), the AdSense
refetch-counting pattern (568-625), the disabled+role-hint test (647-668),
`useConnectorJobActions` tests (`useConnectors.test.tsx:121-218`), and the CONNECTOR CONTROLS
tests (`AppShell.test.tsx:500-550`, `sessionBody` at 306-331). `canRunConnectors` is sourced
from `canRunConnectorJobs` (`AppShell.tsx:123`).

Steps:

- [ ] Write the failing tests. In
  `frontend/src/components/srcc/views/__tests__/ConnectorsView.test.tsx` add:

```tsx
  it("Run pull POSTs report_month + dry_run and shows the submitted banner", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              report_month: "2026-03",
              dry_run: false,
              execution_status: "submitted",
              audit_event: {},
            },
            202,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Manual March pull" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));

    await waitFor(() =>
      expect(screen.getByText(/Submitted/i)).toBeInTheDocument(),
    );
    const jobCall = fetchMock().mock.calls.find(
      ([input, init]) =>
        urlOf(input) === "/connectors/jobs" && methodOf(init) === "POST",
    );
    const body = JSON.parse(
      String((jobCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
    );
    expect(body.report_month).toBe("2026-03");
    expect(body.dry_run).toBe(false);
    expect(body.reason).toBe("Manual March pull");
  });

  it("refetches the runs list after a 202 submitted", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              report_month: "2026-03",
              dry_run: false,
              execution_status: "submitted",
              audit_event: {},
            },
            202,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    const before = runCalls().length;
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Pull then refresh" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
    await waitFor(() => expect(runCalls().length).toBeGreaterThan(before));
  });

  it("surfaces a 409 detail verbatim on Run pull", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            { detail: "A connector job for this scope is already in flight" },
            409,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Duplicate" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
    await waitFor(() =>
      expect(
        screen.getByText(/already in flight/i),
      ).toBeInTheDocument(),
    );
  });

  it("disables Run pull + shows the role hint when the viewer cannot run connectors", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(false);

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /run pull/i }),
    ).toBeDisabled();
    expect(
      screen.getAllByText("Requires a connector-operations role.").length,
    ).toBeGreaterThan(0);
  });
```

  In `frontend/src/lib/api/__tests__/useConnectors.test.tsx` add (inside the
  `useConnectorJobActions` describe block, ~line 217):

```tsx
  it("includes report_month + dry_run in the POST body and resolves submitted", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse(
        {
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          report_month: "2026-03",
          dry_run: true,
          execution_status: "submitted",
          audit_event: {},
        },
        202,
      ),
    );
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });
    let resolved: ConnectorJobResponse | null | undefined;
    await act(async () => {
      resolved = await result.current.requestJob({
        connector_key: "youtube_reporting",
        account_id: "acct-1",
        report_month: "2026-03",
        dry_run: true,
        reason: "Manual pull",
      });
    });
    const [, init] = requireFetchArgs();
    const body = JSON.parse(String((init as RequestInit).body ?? "{}"));
    expect(body.report_month).toBe("2026-03");
    expect(body.dry_run).toBe(true);
    expect(resolved?.execution_status).toBe("submitted");
  });

  it("rejects with a typed 503 when the executor is disabled", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Connector job executor is disabled" }, 503),
    );
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });
    await act(async () => {
      await expect(
        result.current.requestJob({
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          report_month: "2026-03",
          reason: "While disabled",
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 503 });
    });
    expect(result.current.error).toMatchObject({ status: 503 });
  });
```

  In `frontend/src/components/srcc/__tests__/AppShell.test.tsx`, extend the CONNECTOR
  CONTROLS block (after line 529) to assert the Run-pull button respects the gate:

```tsx
  it("CONNECTOR CONTROLS: Run pull disabled when canRunConnectorJobs=false", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(
          sessionBody({ canViewRevenue: true, canRunConnectorJobs: false }),
        ),
      ),
    );
    renderShell();
    fireEvent.click(await screen.findByText("Connectors"));
    const runPull = (await screen.findByRole("button", {
      name: /run pull/i,
    })) as HTMLButtonElement;
    expect(runPull).toBeDisabled();
  });
```

- [ ] Run to fail:
  `cd frontend && npx vitest run src/components/srcc/views/__tests__/ConnectorsView.test.tsx src/lib/api/__tests__/useConnectors.test.tsx src/components/srcc/__tests__/AppShell.test.tsx`
  Expected: failures — no "Run pull" button exists; `ConnectorJobRequestBody` rejects
  `report_month`/`dry_run` under `npx tsc -b`.

- [ ] Minimal implementation. In `frontend/src/lib/api/types.ts`, extend
  `ConnectorJobRequestBody` (lines 513-519):

```ts
export type ConnectorJobRequestBody = {
  connector_key: string;
  account_id: string;
  report_month: string;
  dry_run?: boolean;
  reason: string;
};
```

  Update the `ConnectorJobResponse` doc comment (lines 521-523) to note `execution_status` is
  now `"submitted"` on the executing path (the `string` type is unchanged).

  In `frontend/src/components/srcc/views/ConnectorsView.tsx`:

  Rename the per-row action label to "Run pull" and extend `onRequestSync` (lines 164-177) to
  carry the month + dry_run and refresh runs on success. Add a dry-run state at the component
  top (near line 145, `const [reason, setReason] = useState<string>("");`):

```ts
  const [dryRun, setDryRun] = useState<boolean>(false);
```

  Replace `onRequestSync` (lines 164-177):

```ts
  const onRequestSync = (credential: ConnectorCredential) => {
    const trimmed = reason.trim();
    if (!canRunConnectors || jobActions.loading || !trimmed || !month) return;
    jobActions
      .requestJob({
        connector_key: credential.connector_key,
        account_id: credential.account_id,
        report_month: month,
        dry_run: dryRun,
        reason: trimmed,
      })
      .then((submitted) => {
        // Drop same-tick duplicates (resolve null) and refresh the runs feed
        // only when the run-history panel is mounted.
        if (
          submitted !== null &&
          submitted.execution_status === "submitted" &&
          canViewConnectorHealth
        ) {
          runsReload();
        }
      })
      .catch(() => {
        // The hook already captured the typed error in jobActions.error.
      });
  };
```

  Surface `runsReload` from a lifted run-history reload. Add a `reloadKey` nonce in
  `ConnectorsView` and thread it to `ConnectorSidebar -> RunHistory -> RunHistoryFeed ->
  useRunHistoryFeedState`. Simplest: lift `useRunHistoryFeedState`'s `reload` via a
  callback-ref pattern. Implement by adding a `reloadToken` state in `ConnectorsView`:

```ts
  const [reloadToken, setReloadToken] = useState<number>(0);
  const runsReload = () => setReloadToken((n) => n + 1);
```

  Pass `reloadToken` down: `ConnectorSidebar` (line 205) gains `reloadToken={reloadToken}`;
  `ConnectorSidebar` (lines 358-386) forwards it to `<RunHistory reloadToken={reloadToken} />`;
  `RunHistory` (lines 430-463) forwards `<RunHistoryFeed reloadToken={reloadToken} />`;
  `RunHistoryFeed` (lines 548-555) passes it to `useRunHistoryFeedState(reloadToken)`.
  Then in `useRunHistoryFeedState` (lines 514-541) destructure `reload` from
  `useConnectorRuns(...)` and reset to page 1 when `reloadToken` changes:

```ts
function useRunHistoryFeedState(reloadToken: number): RunHistoryFeedState {
  const [rows, setRows] = useState<ConnectorRun[]>([]);
  const [pagination, setPagination] = useState<ConnectorRunPagination | null>(null);
  const [cursorStartedAt, setCursorStartedAt] = useState<string>();
  const [cursorId, setCursorId] = useState<string>();

  const { data, loading, error, reload } = useConnectorRuns({
    cursor_started_at: cursorStartedAt,
    cursor_id: cursorId,
  });

  // Reset to page 1 and refetch when a new job is submitted.
  useEffect(() => {
    if (reloadToken === 0) return;
    setRows([]);
    setPagination(null);
    setCursorStartedAt(undefined);
    setCursorId(undefined);
    reload();
  }, [reloadToken, reload]);

  useEffect(() => {
    if (!data) return;
    syncRunPage(data, cursorStartedAt, cursorId, setRows, setPagination);
  }, [data, cursorStartedAt, cursorId]);

  const hasMore = Boolean(pagination?.has_more && pagination.next_cursor);
  const nextCursor = pagination?.next_cursor;

  const loadMore = (): void => {
    if (!nextCursor || loading) return;
    setCursorStartedAt(nextCursor.started_at);
    setCursorId(nextCursor.id);
  };

  return { error, runs: rows, hasMore, loading, loadMore };
}
```

  Add the dry-run checkbox to `SyncReasonField` (lines 326-352) — extend it to accept
  `dryRun`/`onDryRun` and render a checkbox after the reason input (kept disabled with the
  same `!canRunConnectors` gate):

```tsx
      <label htmlFor="connectorDryRun" className="item-sub">
        <input
          id="connectorDryRun"
          type="checkbox"
          checked={dryRun}
          disabled={!canRunConnectors}
          onChange={(e) => onDryRun(e.target.checked)}
        />
        {" Dry run (validate only, no facts written)"}
      </label>
```

  Thread `dryRun`/`setDryRun` from `ConnectorsView` -> `DataSourcesPanel` -> `SyncReasonField`
  (add the two props to the panel and field prop lists). Rename the per-row button label in
  `ConnectorCredentialRow` (lines 853-867): change the label text to `"Run pull"` (keep
  `"Working…"` while in flight):

```tsx
          {requestingJob ? "Working…" : "Run pull"}
```

  Update `RequestJobSuccess` (lines 689-711) so the `submitted` branch reads cleanly. The
  existing non-`recorded_not_executed` branch already renders green; make the copy explicit:

```tsx
function RequestJobSuccess({ result }: { result: ConnectorJobResponse }) {
  const submitted = result.execution_status === "submitted";
  const recordedOnly = result.execution_status === "recorded_not_executed";
  const tone = submitted ? "green" : recordedOnly ? "amber" : "green";
  const message = submitted
    ? "Submitted to executor — run history will update"
    : recordedOnly
      ? "Queued (recorded, not yet executed)"
      : result.execution_status;
  return (
    <div className="permission-band" role="status" style={{ margin: 13 }}>
      <Dot tone={tone} />
      <span>
        <strong>Sync requested</strong>
        <span>{`${result.connector_key} · ${result.account_id} — ${message}`}</span>
      </span>
      <Badge tone={tone}>{result.execution_status}</Badge>
    </div>
  );
}
```

- [ ] Run to pass:
  `cd frontend && npx tsc -b && npx vitest run src/components/srcc/views/__tests__/ConnectorsView.test.tsx src/lib/api/__tests__/useConnectors.test.tsx src/components/srcc/__tests__/AppShell.test.tsx`
  Expected: tsc clean; the new Run-pull / refetch / 409 / disabled tests pass, and the
  existing request-job, dedupe, 403, and CONNECTOR CONTROLS tests stay green. Update the
  existing "requests a connector job" test (lines 515-566) if it asserts the old
  `recorded_not_executed` banner copy or the `/request sync/i` button name — point it at the
  `/run pull/i` button and the `submitted` banner (this is the spec'd contract flip; rewrite,
  don't keep both contracts behind one button).

- [ ] Commit:
  ```
  git add frontend/src/lib/api/types.ts frontend/src/components/srcc/views/ConnectorsView.tsx frontend/src/components/srcc/views/__tests__/ConnectorsView.test.tsx frontend/src/lib/api/__tests__/useConnectors.test.tsx frontend/src/components/srcc/__tests__/AppShell.test.tsx
  git commit -m "feat(frontend): Run-pull control (report_month+dry_run, submitted, refetch runs)"
  ```

---

### Task 10: Docs + final validation gate

**Files:**
- Modify: `Docs/12_BACKEND_API_SPEC.md` (POST /connectors/jobs now executes; the 4 telemetry
  fields on the credential read)
- Modify: `Docs/13_SQL_DATA_MODEL.md` (`api_connector_credentials` +4 columns + the CHECK)
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md` and/or `Docs/15_DELIVERY_BACKLOG.md` (inline
  done/remaining marks per the per-PR status rule — no new tracker file)

Steps:

- [ ] Update `Docs/12_BACKEND_API_SPEC.md`. Find the POST /connectors/jobs section and replace
  the `recorded_not_executed` description with the executing contract:
  - Request body: `connector_key`, `account_id`, `report_month` (YYYY-MM, required),
    `dry_run` (bool, default false), `reason` (required, audited).
  - Responses: **202** `{connector_key, account_id, report_month, dry_run,
    execution_status: "submitted", audit_event}` (no `run_id` — the run surfaces in
    `GET /connectors/runs` once the worker commits the RUNNING row); **403** missing
    `connectors.run_jobs`; **503** `"Connector job executor is disabled"`; **422** unknown
    connector / bad month / missing-or-inactive credential; **409** duplicate in-flight
    (in-process registry or a fresh RUNNING row); orphan supersede flips a stale RUNNING row
    FAILED and proceeds to 202 with `superseded_run_id` in the audit details.
  - Audit: one route-owned `CONNECTOR_JOB_RUN` row (`details.action` =
    `job_submitted` | `job_rejected`); the worker emits STARTED/FINISHED edges and a
    `job_failed_before_start` row on Bucket-A failure.
  - GET /connectors/credentials now returns `last_refresh_attempt_at`, `token_expiry_at`,
    `last_refresh_status`, `last_refresh_error_class` (nullable; ISO-8601 timestamps).

- [ ] Update `Docs/13_SQL_DATA_MODEL.md`. In the `api_connector_credentials` table section,
  add the four nullable columns (`last_refresh_attempt_at timestamptz`, `token_expiry_at
  timestamptz`, `last_refresh_status text`, `last_refresh_error_class text`) and the
  `ck_connector_last_refresh_status` CHECK
  (`last_refresh_status IS NULL OR last_refresh_status IN ('succeeded','failed')`). Note the
  table stays tenant-scoped/tenant-writable (no grant change).

- [ ] Update `Docs/01_IMPLEMENTATION_PLAN.md` and/or `Docs/15_DELIVERY_BACKLOG.md` inline:
  mark the connector-jobs executing path + credential refresh telemetry as done, and the
  "POST /connectors/jobs is a no-op" gap as resolved; note the remaining deferrals (no
  celery/redis, no partial-unique-index, no live-OAuth creds, no status auto-flip to
  `failed_auth`).

- [ ] Commit docs:
  ```
  git add Docs/12_BACKEND_API_SPEC.md Docs/13_SQL_DATA_MODEL.md Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
  git commit -m "docs: connector-jobs executor contract + credential telemetry columns"
  ```

- [ ] Final validation gate (the executor runs these — commands only):
  - `python -m ruff check backend tests scripts`
  - Bring up a fresh clean-room Postgres container on `127.0.0.1` (fresh `ums-gate-pg`,
    `postgres:18-alpine`, DB name `test_*`), export `UMS_TEST_DATABASE_URL`, then:
    `python -m pytest -q`
  - `cd frontend && npx tsc -b && npx vitest run`
  - `python scripts/smoke_mvp.py`
  - `git diff --check`

- [ ] If every gate is green, the branch is review-ready. If any gate fails, fix the failure
  (or prove it is unrelated pre-existing debt) before claiming readiness — do not skip,
  xfail, or loosen tests.
