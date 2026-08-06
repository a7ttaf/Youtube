# Scheduled CMS group sync — implementation plan

Spec: `Docs/superpowers/specs/2026-08-06-scheduled-group-sync-design.md`
Branch: `feat/scheduled-group-sync` off `origin/main` at `39523617`
Date: 2026-08-06

## Environment (every task, non-negotiable)

- All Python via `uv run` (bare `python -m` fails — deps live in the uv env).
- Pytest needs
  `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums`
  (PG-tier tests RAISE without it; missing env looks like ~21F/~65E of fake
  regressions). Container `ums-mig-pg-test` must be Up (`docker ps`).
- PG-tier tests must not run concurrently with another pytest session against
  the same container (`_purge_test_rows` is module-scoped).
- Line length: 100 chars hard (DeepSource FLK-E501 ignores the toml's 120).
- Commits: conventional message, NO trailers (no Co-Authored-By — repo
  validation rejects them). Stage file-by-file; never `git add -A` (the tree
  carries pre-existing dirt: `skills-lock.json`, `ci/` line-ending flags,
  untracked `.agents/` — never stage any of it, never touch
  `ci/checks/security.sh`).
- No migration in this PR; alembic head stays `20260805_0001`.

## Anchors (verified against `39523617`)

- Executor: `backend/ums_smart_revenue/connectors/runs/executor.py` —
  registry key `_JobKey = (tenant_id, connector_key, account_id,
  report_month)`; `submit_if_absent` → `activate` / `cancel_reservation`;
  worker precedent `_run_job`; failure-audit precedent
  `_audit_failed_before_start` (fresh session + `platform_lane` +
  `make_placeholder_tenant` RLS bridge); `_build_audit_actor`.
- Sync route: `backend/ums_smart_revenue/api/channels.py:930`
  (`sync_channel_groups`), helpers `_end_credential_transaction` (:1113),
  `_reject_foreign_owner_conflicts` (:1147), `_group_sync_plan_to_api`
  (:1198), `current_groups_client_factory` (:884), `GroupSyncRequest`
  (:876); run-level `GROUPS_SYNCED` summary audit written route-side after
  apply (~:1095).
- Domain: `org/channel_group_sync_apply.py` — `plan_group_sync_with_stores`
  (:130), `apply_group_sync`, per-group `GROUP_UPDATED` audits with
  `details.source = "cms_group_sync"`; UNCHANGED writes nothing.
- Credentials: `connectors/credentials.py` —
  `SqlAlchemyConnectorCredentialRepository.list_credentials(*, limit,
  offset, connector_keys)` (paged, no status filter — filter caller-side);
  `ConnectorCredentialEntry.status: str`.
- Credential resolution: `connectors/runs/orchestrator.py::
  resolve_connector_credentials` (typed errors: `CredentialNotFoundError`,
  `InactiveCredentialError`, `OAuthRefreshError`, broader
  `GoogleConnectorError` family).
- Tenants: `db/tenant_models.py::TenantORM` (`status: Mapped[str]`),
  `tenancy/models.py::TenantStatus` (ACTIVE/SUSPENDED/ARCHIVED); `tenants`
  is NOT in `TENANT_SCOPED_TABLES` (`db/rls.py` guard even excludes it) —
  cross-tenant enumeration needs no tenant context.
- Tenant context replay: `connectors/runs/tenant_context.py::
  connector_tenant_context(tenant_id, *, session)` — raises
  `TenantLifecycleError` unless ACTIVE.
- Service principal: `connectors/google/audit.py::
  build_connector_service_principal(*, tenant_id)` — raises `ValueError`
  when `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` unset.
- Atomic sink: `auth/sql_audit_sink.py::PlatformLaneAuditSink(session, *,
  tenant_id)` — audit rows inside the caller's transaction, platform-lane
  per write.
- Settings: `config/settings.py` — env-name constants at top, `_load_bool`
  / `_load_int` loaders, frozen `AppSettings`.
- App factory: `backend/ums_smart_revenue/app.py` — executor built under
  `if resolved_database_url` + `if settings.connector_job_executor_enabled`
  (:145); lifespan closes executor (:130).
- Tests: `tests/connectors/runs/test_executor.py` (executor unit),
  `tests/api/test_app_connector_executor.py` (boot wiring),
  `tests/api/test_channel_group_sync_api.py` + `_postgres.py` (route — MUST
  NOT CHANGE in this PR), `tests/db/_postgres_helpers.py::
  require_postgres_url`.

## Sequencing

Sched 1 → 2 → 3 → 4 → 5 → 6 sequential (each builds on the previous; the
orchestrator reviews the committed diff between tasks). Sched 7 is the
orchestrator's own validation pass.

---

## Sched 1 — extract `run_group_sync` (opus)

**Files:** new `backend/ums_smart_revenue/connectors/runs/group_sync.py`;
edit `backend/ums_smart_revenue/api/channels.py`; new
`tests/connectors/runs/test_group_sync.py`.

1. Create `connectors/runs/group_sync.py` with a module contract block
   (house style: Purpose / Database-ORM / Standards / Blast Radius /
   Connections):
   - `@dataclass(frozen=True) class GroupSyncRunResult:` `plan:
     GroupSyncPlan`, `execution: GroupSyncExecution | None` (None ⇔
     dry-run).
   - `DEFAULT_GROUPS_CLIENT_FACTORY` (or a `default_groups_client_factory()`
     function): the real `YouTubeGroupsClient` construction, moved here from
     `current_groups_client_factory` in `api/channels.py`, which becomes a
     delegating one-liner. Reason: Sched 2's worker needs the default
     factory without importing `api.*` (layering: `connectors.runs` must
     never import `api`).
   - `run_group_sync(session, *, tenant_id: UUID, content_owner_id: str,
     registry, groups, audit_sink, actor, reason, dry_run: bool,
     client_factory) -> GroupSyncRunResult` owning, in exact route order:
     `resolve_connector_credentials` (typed errors propagate) → end the
     credential transaction (move `_end_credential_transaction` into this
     module) → fetch + snapshot with `try/finally client.close()`
     (`GoogleConnectorError` propagates) → `plan_group_sync_with_stores` →
     dry-run: rollback + return `GroupSyncRunResult(plan, None)` →
     foreign-owner conflict refusal → `apply_group_sync` → return result.
   - `_reject_foreign_owner_conflicts` today raises route-shaped errors —
     READ IT FIRST. The core must raise a typed domain error (reuse an
     existing conflict type if it fits, else a new
     `GroupSyncConflictRefusedError` carrying the exact detail string); the
     route maps it back to the SAME status + SAME detail text so route
     tests pass unmodified. `HTTPException` must not exist in this module.
2. Rewrite the route body as a shell: permission gate → validation
   (unchanged) → `run_group_sync(...)` → error-to-HTTP mapping (credential
   503 trio + broad `GoogleConnectorError` 503 for the credential layer,
   fetch 502, conflict-pair 409 — byte-identical details) → rendering +
   the run-level `GROUPS_SYNCED` summary audit row (STAYS route-side,
   unconditional, exactly as today).
   Nuance: today the credential-layer 503s and the fetch 502 are
   distinguished by WHERE the `GoogleConnectorError` is raised (resolution
   vs fetch). After extraction both propagate from `run_group_sync`, so the
   core must let the route tell them apart — e.g. wrap the fetch phase's
   `GoogleConnectorError` in a typed `GroupSyncFetchError` (from exc) while
   credential-phase errors propagate raw. Pick the minimal mechanism that
   keeps every existing route test's status/detail assertion passing
   unmodified.
3. Red-first: new `tests/connectors/runs/test_group_sync.py` — dry-run
   returns `execution=None` and rolls back (session spy); typed errors
   propagate untranslated (no HTTPException anywhere); apply path returns
   the execution and leaves the summary-audit responsibility to the caller
   (assert the core wrote NO `GROUPS_SYNCED`-type row itself — fake sink).
4. **Hard gate:** `uv run pytest tests/api/test_channel_group_sync_api.py
   tests/api/test_channel_group_sync_postgres.py -q` green with ZERO edits
   to those two files — prove with `git diff --stat` showing they are
   untouched. Also run `tests/connectors/runs/` and `tests/org/`.

Commit: `refactor(connectors): extract run_group_sync core from the sync route`

## Sched 2 — sync jobs on the executor (opus)

**Files:** edit `connectors/runs/executor.py`; new tests (extend
`tests/connectors/runs/test_executor.py` or new
`tests/connectors/runs/test_group_sync_jobs.py`).

1. Constants: `GROUP_SYNC_JOB_CONNECTOR_KEY = "cms_group_sync"`,
   `GROUP_SYNC_JOB_MONTH = "-"`. Key shape
   `(tenant_id, GROUP_SYNC_JOB_CONNECTOR_KEY, content_owner_id,
   GROUP_SYNC_JOB_MONTH)` — collision with pulls impossible (connector_key
   namespace).
2. Executor ctor gains `group_sync_client_factory` keyword arg defaulting
   to the Sched-1 default factory — the PG tier and unit tests inject fakes
   here; production passes nothing.
3. `submit_group_sync_if_absent(*, tenant_id, content_owner_id,
   actor_identity) -> _SlotReservation | None` reusing the registry lock +
   reserve flow; `activate` works on it unchanged (worker dispatch decided
   by the reservation's connector_key — extend `_SlotReservation` with a
   `job_kind` field or branch on the sentinel key; pick the cleanest that
   keeps `activate` single-pathed).
4. `_run_group_sync_job(*, tenant_id, content_owner_id, actor_identity)`:
   own session → `connector_tenant_context(tenant_id, session=session)` →
   actor = `build_connector_service_principal(tenant_id=...)` extended with
   `MANAGE_GROUPS@global` (fabricate via a small local helper mirroring
   `_build_audit_actor`; the audit row must honestly carry the authority
   exercised — `GROUPS_SYNCED`/`GROUP_UPDATED` declare `MANAGE_GROUPS`) →
   stores: the SQL registry + groups stores the api dependencies construct
   (verify exact classes from `api/channels.py` dependency helpers; import
   the STORE classes, not the api module) → sink =
   `PlatformLaneAuditSink(session, tenant_id=tenant_id)` →
   `run_group_sync(..., dry_run=False, reason="scheduled CMS group sync",
   client_factory=self._group_sync_client_factory)` → if executed counts
   contain any non-UNCHANGED outcome: write the run-level `GROUPS_SYNCED`
   summary row (same field shape as the route's) → single `session.commit()`
   (domain rows + audit rows one transaction — the #169 invariant by
   construction). UNCHANGED-only: no summary row, log line only, still
   commit (harmless; nothing pending).
5. Failure taxonomy: `except` the typed families
   (`TenantLifecycleError`, `GoogleConnectorError` incl. credential trio,
   the conflict pair, `GroupSyncFetchError`) → one `CONNECTOR_JOB_RUN` row,
   `action="group_sync_job_failed"`, `error_class=type(exc).__name__`
   (NEVER `str(exc)`), via a sibling of `_audit_failed_before_start`
   (fresh session, `platform_lane`, placeholder-tenant RLS bridge; reuse
   `_build_audit_actor`-style principal). Catch-all `except Exception`:
   log, never escape the thread. `finally: _deregister(key)`.
6. Red-first tests: registry-key non-collision (a pull job and a sync job
   for the same tenant coexist); dedup (second submit returns None);
   happy path with fake factory + real SQLite session (group rows +
   per-group `GROUP_UPDATED` rows + summary `GROUPS_SYNCED` row land, one
   commit); UNCHANGED-only → ZERO audit rows, with the anti-vacuity twin
   (same fixture, one changed group → rows land); one test per failure
   row in the spec's taxonomy table (fake raising at each seam) asserting
   the `group_sync_job_failed` row and its `error_class`.

Commit: `feat(connectors): cms group-sync job kind on the connector executor`

## Sched 3 — the scheduler (sonnet)

**Files:** new `connectors/runs/scheduler.py`; new
`tests/connectors/runs/test_scheduler.py`.

1. `GroupSyncScheduler(*, session_factory, executor, interval_seconds)`:
   `threading.Event` stop-flag; daemon thread
   `while not self._stop.wait(self._interval): self._tick_safely()`; first
   tick one full interval after `start()`; `close()` = set + join;
   `weakref.finalize` GC backstop (executor/resolver precedent).
2. `tick()`: own session → `select(TenantORM).where(TenantORM.status ==
   TenantStatus.ACTIVE.value)` (no tenant context needed — `tenants` is
   unscoped; add this rationale as a comment citing `db/rls.py`) → per
   tenant, inside try/except (fault isolation — log + continue):
   `connector_tenant_context(tenant.id, session=session)` →
   `SqlAlchemyConnectorCredentialRepository(session,
   tenant_id=tenant.id).list_credentials(connector_keys=
   frozenset({YOUTUBE_ANALYTICS_CONNECTOR}))` — PAGE with the loop until
   `has_more` is False; filter entries to the ACTIVE status literal (verify
   the exact literal from `resolve_connector_credentials` /
   `InactiveCredentialError` and use the same constant) → per entry:
   `submit_group_sync_if_absent(tenant_id=..., content_owner_id=
   entry.account_id, actor_identity=...)`; None → debug log (in-flight);
   reservation → `executor.activate(reservation)` immediately (no route
   audit to defer behind — comment why this differs from the route's
   after-commit dance).
   `actor_identity`: `ConnectorJobActor` built from the service actor id
   (settings) — tick fails fast with a clear log if unset (boot prevents
   this; belt-and-braces).
   `_tick_safely()`: catch-all wrapper — a failed tick logs and waits for
   the next interval; the thread NEVER dies.
3. Red-first tests (drive `tick()` directly; no clocks, no sleeps): two
   ACTIVE + one SUSPENDED tenant with mixed credentials (youtube-analytics
   active, youtube-analytics inactive, other-connector active) → exactly
   the expected (tenant_id, content_owner_id) set submitted (fake executor
   recorder); pagination exercised (page-size-1 fixture or repo fake);
   first-tenant-raises → second still submitted; in-flight (fake returns
   None) → no activate call; start/close lifecycle (thread starts, close
   joins promptly); catch-all keeps a poisoned tick from killing the loop.

Commit: `feat(connectors): in-process group-sync scheduler`

## Sched 4 — settings + boot wiring (sonnet)

**Files:** edit `config/settings.py`, `backend/ums_smart_revenue/app.py`;
new `tests/api/test_app_group_sync_scheduler.py` (mirror
`test_app_connector_executor.py` patterns); possibly extend
`tests/config/` settings tests.

1. Settings: `GROUP_SYNC_SCHEDULE_ENABLED_ENV =
   "UMS_GROUP_SYNC_SCHEDULE_ENABLED"`, `GROUP_SYNC_INTERVAL_HOURS_ENV =
   "UMS_GROUP_SYNC_INTERVAL_HOURS"`; `AppSettings.group_sync_schedule_enabled:
   bool = False`, `group_sync_interval_hours: int = 24`; loaders via
   `_load_bool` / `_load_int` (positive-int validation like max_workers).
2. `create_app`: inside the `resolved_database_url` block, after the
   executor block — when `settings.group_sync_schedule_enabled`:
   - executor disabled → `raise ValueError(...)` naming both env vars;
   - `settings.google_connector_service_actor_id is None` →
     `raise ValueError(...)` naming the env var;
   - else build `GroupSyncScheduler(session_factory=...,
     executor=_app.state.connector_job_executor,
     interval_seconds=hours * 3600)`, `start()`, stash on
     `_app.state.group_sync_scheduler`.
   No database URL → the whole block is skipped (inert flag — matches the
   executor's own behaviour; comment it).
3. Lifespan finally: close scheduler FIRST (stop ticking), then executor
   (drain) — extend the existing finally with the same getattr-guarded
   pattern.
4. Red-first tests: enabled-without-executor refuses to build; enabled-
   without-service-actor refuses; enabled-with-both builds, thread present,
   lifespan shutdown closes both (scheduler before executor — assert via
   recorded close order with monkeypatched closes); disabled (default) →
   no scheduler attribute, no thread. Settings-loader tests: defaults,
   truthy parsing, zero/negative hours → ValueError.

Commit: `feat(app): boot wiring + fail-fast settings for scheduled group sync`

## Sched 5 — Postgres tier (opus)

**Files:** new `tests/api/test_scheduled_group_sync_postgres.py` (house
PG-tier style: `require_postgres_url`, module purge, real engine).

1. End-to-end convergence: seed ACTIVE tenant + active youtube-analytics
   credential row + a fake CMS snapshot via the executor's injected
   `group_sync_client_factory` (build a real `ConnectorJobExecutor` with
   the fake) → `GroupSyncScheduler.tick()` → wait on the job future
   (`activate` returns it; fakes make it fast) → assert on the REAL engine:
   `channel_groups` row with the owner stamp, membership rows, per-group
   `GROUP_UPDATED` audit rows + `GROUPS_SYNCED` summary row, all under the
   correct tenant_id (RLS lanes exercised for real).
2. Converged re-tick: run `tick()` again with the same snapshot → job runs
   → ZERO new audit rows (count before/after), groups unchanged.
3. Failure cross-lane proof: tenant with NO credential → tick submits →
   worker fails → exactly one `CONNECTOR_JOB_RUN` row with
   `action="group_sync_job_failed"`,
   `error_class="CredentialNotFoundError"` on the real engine (the
   platform-lane + placeholder-tenant RLS bridge proven for the new path).
4. Respect the serial-use container rule; reuse credential-seeding helpers
   from existing connector PG tests if present (search before writing new).

Commit: `test(connectors): postgres tier for scheduled group sync`

## Sched 6 — trackers + API doc (sonnet)

- `Docs/15_DELIVERY_BACKLOG.md`: extend the CMS-group-sync entry —
  scheduled convergence shipped (executor job kind + in-process scheduler,
  fail-closed OFF, credential-list-as-registry).
- `Docs/01_IMPLEMENTATION_PLAN.md`: one-line status note on the
  group-mapping item; Status header date.
- `Docs/12_BACKEND_API_SPEC.md`: scheduled-mode paragraph in the sync
  section (settings, service actor, audit taxonomy incl. the
  summary-row-only-on-change rule). No new endpoints.

Commit: `docs: record scheduled group sync in trackers and API spec`

## Sched 7 — full validation (orchestrator)

With `UMS_TEST_DATABASE_URL` set and the container up, nothing else using it:

1. `uv run pytest -q` — full suite, exit 0.
2. `uv run ruff check backend tests` and `uv run ruff format --check`.
3. `uv run mypy` on every backend module this PR touched.
4. 100-char guard over changed files.
5. `uv run alembic heads` → single head `20260805_0001` (no migration).
6. `git diff --check` clean.
7. Verify the two protected route-test files are untouched:
   `git diff origin/main --stat -- tests/api/test_channel_group_sync_api.py
   tests/api/test_channel_group_sync_postgres.py` → empty.

Then the ONE batched ask: push + open PR.

## Subagent protocol (unchanged from the last three PRs)

Fresh agent per task; prompts carry: the environment block above, the
task's full text, the anchors it needs, red-first TDD evidence requirement
(show the failing run before the fix), STATUS: DONE / DONE_WITH_CONCERNS /
BLOCKED with evidence, commit rules. The orchestrator reviews every diff
between tasks; review findings get implemented, not argued
("consistent with surrounding code" is not a defence).
