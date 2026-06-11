# Connector Jobs Executing Path — Design Spec

Date: 2026-06-11
Status: DRAFT — awaiting operator review (no implementation on this branch)
Branch: `spec/connector-jobs-executor` (off main `82fd67f`, PR #93)
Author lane: architect/reviewer (Claude), evidence-verified against main `82fd67f`

## Problem

`POST /connectors/jobs` is a no-op recorder. It validates permission, writes one
`CONNECTOR_JOB_RUN` audit row with `details={"action": "job_request_recorded"}`, and returns
202 with the hardcoded literal `execution_status: "recorded_not_executed"`
(`backend/ums_smart_revenue/api/connectors.py:261-284`). It never calls `run_one`. The only
production trigger of a live Google pull is the CLI `scripts/run_google_connector.py`. An
operator using the dashboard cannot start an ingest.

The 2026-06-10 ingestion spec explicitly deferred this: "`run_one` is synchronous and
network-bound (live Google API + OAuth + blob I/O); an API-triggered pull needs
background/async execution infrastructure and a `report_month` field on
`ConnectorJobRequest`. That is a materially larger, independent change → its own follow-up
spec." This is that spec.

## Current-state evidence (all refs at main `82fd67f`)

- **Route**: `request_connector_job` gated by `RUN_CONNECTOR_JOBS @ AccessScope.connector(key)`
  (`api/connectors.py:268-269`). Request model `ConnectorJobRequest` has `connector_key`,
  `account_id`, `reason` — **no `report_month`, no `dry_run`** (`api/connectors.py:72-77`).
  Pinning test: `tests/api/test_connectors_api.py:252-279` asserts 202 +
  `"recorded_not_executed"` + exactly one audit row.
- **run_one**: `run_one(session, *, tenant_id, connector_key, account_id, report_month,
  dry_run=False, triggered_by_user_id=None) -> ConnectorRunOutcome`
  (`connectors/runs/orchestrator.py:382-391`). Fully synchronous; owns mid-flight
  `session.commit()`/`rollback()` calls (RUNNING row committed immediately at start, a commit
  per successful report, terminal commit, post-run normalization commit). Wall time: minutes
  to tens of minutes (httpx sync, read timeout 60s × retry budgets ×
  per-report downloads + one Analytics query per target channel). **It must never share a
  request session** — `authenticated_session_dependency` commits/rolls back at request end
  (`db/session.py:199-214`), and run_one's rollbacks would discard unrelated request writes.
- **Failure taxonomy**: Bucket A (validation/credential/OAuth-refresh failures) raises
  *before* `start_run` → **zero DB trace** (no run row, no audit). Bucket B: per-report
  failures → PARTIAL. Bucket C: run-level errors → run rewritten FAILED + reraise. Post-run
  projection failure: run rewritten FAILED + `PROJECTION_FAILED` audit + reraise
  (`connectors/runs/normalization.py:114-137`).
- **No concurrency guard**: `connector_runs` has no uniqueness on
  `(tenant_id, connector_key, account_id, report_month)` — only a non-unique index. Two
  concurrent `run_one` calls for the same scope both insert RUNNING rows and collide only at
  the source-row upsert (last-write-wins / possible deadlock) (`db/connector_models.py:116-121`).
- **No background machinery**: no lifespan, no startup hooks, no BackgroundTasks usage.
  `celery==5.6.3` + `redis==8.0.0` are pinned in `pyproject.toml` but have **zero imports**
  anywhere (pre-provisioned, dead). The only bounded-blocking precedent is
  `TenantResolverMiddleware`'s private `ThreadPoolExecutor(max_workers=8)` + semaphore
  (`tenancy/resolver.py:113-127`). The `ExportJobORM` "QUEUED" precedent is
  deferred-synchronous (materialized in-request at download), not a worker.
- **Tenant RLS constraint (Track E)**: the session hook reads `TENANT_CTX` (a contextvar) on
  every transaction begin (`db/session.py:153-197`). A manually spawned thread starts with a
  fresh contextvars context → `TENANT_CTX=None` → context cleared → tenant-table reads come
  back EMPTY (fail-closed). Any worker thread must explicitly set `TENANT_CTX` (or run under
  `contextvars.copy_context()`).
- **VERIFIED BLOCKER (2026-06-11 deep audit, adversarially confirmed P1+P2)**: the ingestion
  pipeline currently composes end-to-end **only on SQLite**. Two stacked problems on
  RLS-enforced Postgres:
  1. **P1** — `scripts/run_google_connector.py:186` (the only production trigger) builds a
     tenant-lane session but never sets `TENANT_CTX`, so `app_current_tenant_id()` is NULL,
     every tenant-table policy denies all rows, and `run_one` dies at `_load_credential` with
     a misleading `CredentialNotFoundError` (exit 2) before any ingest. Fail-closed — no leak,
     no wrong write — but the merged #90+#93 deliverable cannot execute against the Postgres
     source of truth, and the all-SQLite test suite cannot see it (the session hook no-ops off
     Postgres).
  2. **P2** — even with context set, the run path writes three **platform-only-write** tables
     on the tenant lane: `audit_logs` (run-lifecycle emits at `orchestrator.py:753-761`,
     normalization audit at `normalization.py:66-105`), `monthly_channel_revenue_facts`
     (`record_fact`), and `finance_month_close` (`get_or_create_month_close_row` INSERT at
     `google_source_normalizer.py:217-222`). Track-E migration `20260608_0001` grants
     `app_tenant` no DML on those tables (`TENANT_PLATFORM_ONLY_WRITE_TABLES`, pinned by
     `tests/tenancy/test_rls_grant_surface.py:117-135`). Compounding contract break: when the
     fact INSERT denies, `_record_projection_failure_on_run`'s own audit INSERT also denies
     and rolls back the FAILED rewrite — the durable `connector_runs` row stays **SUCCEEDED
     with zero facts projected**. API routes solved this lane split with platform-bound
     dependencies (`api/dependencies_finance.py:38-48`) and the explicit
     `SET LOCAL ROLE app_platform` elevation precedent in `committed_allocation.py:247`; the
     connectors package has zero elevation.

  These are **prerequisites** for this spec — see the next section.
- **Service actor**: a live run requires `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` (fail-closed
  ValueError before `start_run`) and blob config (`UMS_BLOB_BACKEND`).
- **triggered_by FK**: composite FK `(tenant_id, triggered_by_user_id) → users(tenant_id, id)`
  — an API-supplied user UUID must exist as a users row in that tenant or `start_run` violates
  the FK. The API path has a real authenticated principal, but service/header principals may
  not exist in `users`; the executor must degrade to `triggered_by_user_id=None` when the
  principal's UUID is not a tenant users row (attribution preserved in the audit `reason` and
  actor stash; `SqlAlchemyAuditSink` already stashes unknown actors in
  `details["actor_user_id"]`).

## Prerequisite PR (ships FIRST, own branch): ingestion RLS lane fix

The P1/P2 blockers above predate this spec (P1 landed with Track E #85; P2 became load-bearing
when #90 wired facts into the run path) and they break the **CLI** today, independent of any
executor. Recommended as a separate, small PR so the fix is not held hostage by executor
review:

1. **Tenant context**: a shared helper (e.g. `connectors/runs/tenant_context.py`) that sets
   `TENANT_CTX` for the target tenant around `run_one` and resets in `finally`; used by the
   CLI now, by the executor worker later. (`FIX:` comment per standard.)
2. **Lane discipline for the platform-only writes**: scoped `SET LOCAL ROLE app_platform`
   elevation around the three platform-only write surfaces in the run path (run-lifecycle
   audit emits, normalization fact+audit+month-close writes), following the
   `committed_allocation.py:247` precedent — NOT a blanket platform session for the whole run
   (source-row/connector-run writes stay on the tenant lane so RLS keeps doing its job).
   Alternative considered: granting `app_tenant` DML on the three tables — **rejected**, it
   widens the tenant lane's blast surface and contradicts Track E's pinned grant model.
3. **A PG-tier test that exercises `run_one` against RLS** (the entire current coverage of
   this path is SQLite; that is exactly why two green full-suite runs never caught this).
4. Rides along (both verified P3, same files): fix the stale
   `ConnectorRunOutcome.analytics_cleanup_blocked` docstrings (`orchestrator.py:200-207`,
   `:846-852` claim the normalize gate consumes the flag; the gate read was removed in
   `a3a584a` — either re-consume the flag in the gate as defense-in-depth or correct both
   docstrings and pin the intended semantics with a test), and the Docs/12 + Docs/13
   ingestion drift (Docs/13:241 still calls `google_revenue_source_rows` "planned";
   Docs/12:364-367 still calls fact normalization "future"; `recorded_not_executed` and the
   run-driven REPORT_IMPORTED / PROJECTION_FAILED semantics are undocumented).

This spec's executor worker then simply reuses items 1-2 (same helper, same elevation
pattern) instead of inventing its own session discipline.

## Approaches considered

**A. In-process bounded executor (RECOMMENDED).** A small module-owned
`ThreadPoolExecutor` (default `max_workers=1`) wired in `create_app`, mirroring the
`TenantResolverMiddleware` precedent. `POST /connectors/jobs` validates cheaply in-request,
submits the pull, returns 202 immediately; progress/status is observable through the existing
`GET /connectors/runs` reader (no new read surface).
*Pros*: no new infrastructure or ops burden; smallest change; reuses the repo's only
threading precedent; run durability already exists (`start_run` commits the RUNNING row
up-front; terminal rewrite + sweep already handle in-process failure).
*Cons*: queued-but-not-yet-started jobs die silently with the process (only started runs
leave rows); per-process queue under multi-worker uvicorn; needs explicit `TENANT_CTX` and
session discipline in the worker.

**B. Activate celery + redis.** Durable queue, retries, scheduling; deps already pinned.
*Rejected for now*: greenfield activation (worker process, broker, deployment topology) with
a blast radius far beyond the current single-process operational phase; the pinned-but-unused
deps signal "provisioned for later", not "use me now". Approach A's API contract (202 +
runs-reader polling) is forward-compatible with a later celery swap.

**C. Synchronous in-request execution.** Run `run_one` inside the request thread.
*Rejected*: minutes-long HTTP requests; gateway/client timeouts; a client abort does not stop
the worker anyway (the run completes detached) — all of the cost, none of the control.

**D. Deferred-synchronous (ExportJob pattern).** Record QUEUED and materialize "on read".
*Rejected*: a pull has no natural materialization moment; a poll-triggered multi-minute pull
inside a GET is approach C with extra steps.

## Design (Approach A)

### New module: `backend/ums_smart_revenue/connectors/runs/executor.py`

`ConnectorJobExecutor` — owns:

- a `ThreadPoolExecutor(max_workers=settings.connector_job_max_workers, thread_name_prefix="ums-connector-job")`,
  default **1** (serialized pulls; Google quotas and the source-row collision surface make
  parallel pulls undesirable by default);
- an in-process registry `dict[(tenant_id, connector_key, account_id, report_month), Future]`
  guarded by a `threading.Lock` — the primary duplicate-submission guard;
- a `submit(...) -> SubmissionResult` API used by the route, and a `shutdown()` for tests.

Worker body (each submitted job):

1. Build a fresh session from the executor's own `session_factory`
   (`build_session_factory(database_url)` — per-URL engine cache means no second pool).
2. Establish tenant + lane context via the **prerequisite PR's shared helper** (sets
   `TENANT_CTX` for the submitting tenant inside the worker thread — a fresh contextvars
   context otherwise fail-closes RLS — and carries the platform-lane elevation for the
   run path's platform-only writes); reset in `finally`.
3. `run_one(session, tenant_id=..., connector_key=..., account_id=..., report_month=...,
   dry_run=..., triggered_by_user_id=<resolved or None>)`.
4. Catch and log (never propagate out of the thread): `GoogleConnectorError` (Bucket A — see
   audit note below), projection reraise (run row already FAILED + audited), unexpected
   exceptions (`logger.exception`).
5. Remove the registry entry in `finally`.

**Bucket-A visibility fix**: today a pre-start failure (missing/inactive credential, secret
resolution, OAuth refresh) leaves zero DB trace. In the executor path nobody is watching a
terminal either, so the worker's Bucket-A catch writes one `CONNECTOR_JOB_RUN` audit row with
`details={"action": "job_failed_before_start", "error_class": type(exc).__name__}` (canned
class name only — no exception text, matching the test route's no-interpolation rule) on its
own short-lived session. This is additive observability, not a behavior change to `run_one`.

### Route changes: `POST /connectors/jobs`

- `ConnectorJobRequest` grows `report_month: str` (required; validated with
  `validate_report_month` → 422 on bad format) and `dry_run: bool = False`.
- Gate unchanged: `RUN_CONNECTOR_JOBS @ AccessScope.connector(connector_key)` — fail-closed,
  same permission the CLI's service principal carries. No new permission.
- In-request, pre-submission validation (cheap, no network):
  1. `connector_key` must be registered (`registry.known_keys()`) → 422.
  2. Credential row must exist and be `active` for `(tenant, key, account)` → 422
     (`job_rejected` audit, see below). This duplicates `run_one`'s own check by design: it
     converts the most common Bucket-A failure into a synchronous, actionable 4xx instead of
     a silent background death.
  3. Duplicate guard (below) → 409.
- On accept: submit to the executor, write the `CONNECTOR_JOB_RUN` audit row with
  `details={"action": "job_submitted", "report_month": month, "dry_run": dry_run}`, return
  **202** with `execution_status: "submitted"` plus the echoed job scope. The
  `"recorded_not_executed"` literal disappears — an intentional, spec'd contract change; the
  pinning test is rewritten (not loosened) to pin the new contract.
- Rejections (422/409) write a `CONNECTOR_JOB_RUN` audit row with
  `details={"action": "job_rejected", "rejection": <canned reason token>}` — submission
  attempts on the ingest path are audit-worthy whether or not they run.

### Duplicate-run guard

Primary: the in-process registry — a second submission for the same
`(tenant, key, account, month)` while a Future is live → **409 Conflict**.
Secondary (cross-process / post-crash): a submission-time SELECT for an existing RUNNING
`connector_runs` row for the same scope:

- RUNNING row younger than `settings.connector_job_stale_running_hours` (default 6) → 409.
- RUNNING row older than the threshold → treated as an orphan from a dead process: the
  submission marks it FAILED (`error_summary="orphaned RUNNING run superseded by new job"`,
  via the existing terminal-transition repository path) and proceeds. This keeps a crashed
  process from permanently blocking a scope.

A Postgres partial unique index (`WHERE status = 'RUNNING'`) was considered and **deferred**:
it hard-blocks the scope on any orphaned row until manual surgery, which is worse
operationally than the supersede rule; revisit if multi-process deployment lands before a
real queue does.

### Status / read surface

No new read endpoint. `GET /connectors/runs` (existing, gated by VIEW_CONNECTOR_HEALTH)
already shows the RUNNING→terminal lifecycle, counts, and sanitized error summaries.
The dashboard Connectors screen already consumes it. A queued-but-not-started job is visible
only via the submit 202 (and audit log) until `start_run` commits — acceptable at
`max_workers=1` queue depths; documented in the route contract block.

### Frontend (same PR, thin)

Connectors screen: a "Run pull" action on a credential row (month picker + reason
+ optional dry-run), POSTing to `/connectors/jobs`; on 202, refresh the runs list. Disabled
without `connectors.run_jobs` capability (session-hydration capabilities already exist).
Errors surface the 409/422 detail verbatim. No new client state machinery.

### CLI fix

Moved to the **prerequisite PR** (see section above) — the CLI is broken on RLS-enforced
Postgres today, executor or not, so the fix must not wait on this spec's review cycle. The
executor worker reuses the same helper.

### Settings (all `config/settings.py`, env-prefixed `UMS_`)

- `connector_job_executor_enabled: bool = False` — **fail-closed default OFF**. When
  disabled, `POST /connectors/jobs` returns **503** `"Connector job executor is disabled"`
  (explicit refusal — NOT a silent fallback to the old recorder behavior, which would put two
  contracts behind one route). Flip ON per environment once real creds exist.
- `connector_job_max_workers: int = 1`.
- `connector_job_stale_running_hours: int = 6`.

## Part 2 (separable; same PR by default, own commits): credential refresh telemetry

The executor makes credential failures *less* visible than the CLI (no stderr terminal), and
today nothing persists refresh health: `api_connector_credentials` has **no**
`token_expiry`/`last_refresh_at`/`last_error` columns; `failed_auth`/`rotating`/`disabled`
statuses are CHECK-allowed but write-dead (no updater exists anywhere); google-auth's
in-memory `credentials.expiry` is discarded after every run.

Minimal additive schema (one Alembic migration, nullable columns, no backfill needed):

- `last_refresh_attempt_at: timestamptz NULL`
- `last_refresh_status: text NULL` (CHECK `IN ('succeeded','failed')`)
- `last_refresh_error_class: text NULL` (canned exception class name only — never message
  text, which can embed URIs)
- `token_expiry_at: timestamptz NULL` (from `credentials.expiry` after a successful refresh)

Write points (both already hold the row in-session):

1. `resolve_connector_credentials` (`orchestrator.py:609-643`) — the single chokepoint where
   refresh outcome is known; stamp on success and failure (commit on the caller's session is
   owned by the existing flow; the stamp must not change Bucket-A semantics — stamp+commit in
   a `finally`-style short transaction so a raised `OAuthRefreshError` still propagates).
2. The test route (`api/connectors.py:325-360`) — stamps the same fields, so "Test
   connection" finally persists something an operator can see.

Read surface: `ConnectorCredentialEntry` gains the four fields (additive). No status
auto-flip to `failed_auth` in this PR (quarantine semantics are a policy decision — listed as
an open question).

## Blast-radius review (mandatory checklist)

- **Tables/ORM affected**: Part 1 — none (no schema change; new code paths only write
  existing `connector_runs` + `audit_logs` rows through existing repositories). Part 2 —
  `api_connector_credentials` (+4 nullable columns, additive migration; ORM
  `ApiConnectorCredentialORM`).
- **PostgreSQL still source of truth**: yes — the executor only triggers the existing
  `run_one` → source rows → normalization → facts chain; no new write path bypasses
  `record_fact`/the source-row upsert.
- **Existing migrations/tests/seed/docs break?**: the `/connectors/jobs` pinning test is
  intentionally rewritten per this spec (contract change, documented in Docs/12). Part 2
  migration is additive; downgrade drops the four columns.
- **Neo4j projection impact**: No graph projection impact detected — fact shapes unchanged;
  no graph code exists in `backend/` (verified by the #93 review sweep).
- **Authorization more permissive?**: no — same `RUN_CONNECTOR_JOBS@connector` gate; the
  disabled-executor path is 503 fail-closed; VIEW_CONNECTOR_HEALTH read gate untouched.
  Worker uses the existing tenant-pinned service-principal convention for actor attribution.
- **Finance/locks/overrides**: untouched — the post-run projection keeps its 3-layer
  locked-month fail-closed behavior (re-verified on `82fd67f` by the #93 review).
- **Audit**: additive `CONNECTOR_JOB_RUN` `details.action` values (`job_submitted`,
  `job_rejected`, `job_failed_before_start`) following the existing
  lifecycle-discriminator convention; `audit_logs.event_type` is plain Text so no migration.
- **Rollback/reset note**: Part 1 has none (config-off restores 503). Part 2 downgrade is a
  pure column drop; pre-alpha data disposable.

## Test plan (TDD; all new behavior pinned)

Backend (`tests/api/test_connectors_api.py`, new `tests/connectors/runs/test_executor.py`):

- 202 submit happy path: executor receives exact kwargs (patched), audit `job_submitted`
  row, response shape.
- 422: bad month, unknown connector key, missing/inactive credential (+ `job_rejected` audit).
- 409: live in-process duplicate; RUNNING-row duplicate younger than threshold.
- Orphan supersede: stale RUNNING row older than threshold → marked FAILED + new job accepted.
- 503 when `connector_job_executor_enabled=False`.
- Authz matrix: missing permission 403 fail-closed; connector-scoped grant for a different
  connector 403; disabled user 403.
- Worker: sets and resets `TENANT_CTX`; uses its own session (request session untouched);
  Bucket-A failure writes `job_failed_before_start` audit and never propagates;
  projection-reraise is swallowed-after-audit (run already FAILED); registry entry removed on
  all paths.
- CLI: `TENANT_CTX` set around `run_one` (unit-level assert on the contextvar).
- Part 2: stamps on refresh success/failure at both write points; `OAuthRefreshError` still
  propagates after a failure stamp; entry serialization includes the new fields; migration
  round-trip (PG tier).

Frontend: submit action gated by capability; 202 refreshes runs list; 409/422 surfaced.

## Validation plan

`python -m ruff check backend tests scripts` · full `python -m pytest -q` on a fresh
clean-room Postgres container (`127.0.0.1`, fresh `ums-gate-pg`) · frontend `npx tsc -b` +
`vitest run` · `python scripts/smoke_mvp.py` · `git diff --check` · targeted:
`tests/api/test_connectors_api.py`, `tests/connectors/runs/`, PG migration tests for Part 2.

## Open questions for the operator

0. **Green-light the prerequisite RLS lane-fix PR first?** (Recommended — it fixes a verified
   P1 on the merged deliverable and is independent of this spec's review.)
1. **Ship Part 2 (credential telemetry + migration) in the same PR or split?** Default in
   this spec: same PR, separable commits.
2. **Executor default-enabled in dev but disabled in prod-like env?** Default here: OFF
   everywhere until real OAuth creds exist (the live pull is creds-blocked anyway).
3. **Auto-flip credential `status` to `failed_auth` on refresh failure** (quarantine) — out
   of scope here; wants its own decision because it can disable ingestion paths on a single
   transient failure.

## Non-goals

- No celery/redis activation, no scheduler, no retry policy (one submission = one attempt).
- No PAYMENT-grain work, no live-OAuth credential provisioning (still creds-blocked).
- No change to `run_one`'s internal semantics or the #93 normalization gates (the zero-
  mutation-gate spec drift is a separate one-line doc fix on the 2026-06-10 spec).
