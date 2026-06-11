# Ingestion RLS lane fix — make the merged pipeline executable on Postgres

Date: 2026-06-12
Status: Locked design (prerequisite carved out of the 2026-06-11 connector-jobs executor
spec after the deep audit); implementation on this branch.
Branch: `fix/ingestion-rls-lane` (off main `82fd67f`, PR #93)

## Problem (both findings adversarially verified on main `82fd67f`)

The merged ingestion pipeline (#90 normalize wiring + #93 adapter) composes end-to-end
**only on SQLite** — and the all-SQLite test coverage of this path is exactly why two green
full-suite runs never caught it:

1. **P1 — CLI dead under RLS.** `scripts/run_google_connector.py` (the only production
   trigger; `POST /connectors/jobs` is still `recorded_not_executed`) builds a tenant-lane
   session but never sets `TENANT_CTX`. On Postgres the `after_begin` hook
   (`db/session.py:153-196`) finds no tenant, clears the trusted context row, and pins
   `app_tenant`; `app_current_tenant_id()` returns NULL, every tenant-table policy denies all
   rows, and `run_one` dies at `_load_credential` with a misleading
   `CredentialNotFoundError` (exit 2) before any ingest. Fail-closed, but the deliverable
   cannot run against the source of truth.
2. **P2 — platform-only writes on the tenant lane.** Even with context set, the run path
   writes three tables that Track-E migration `20260608_0001` grants **no** `app_tenant`
   DML on (`TENANT_PLATFORM_ONLY_WRITE_TABLES`, pinned by
   `tests/tenancy/test_rls_grant_surface.py:117-135`):
   - `audit_logs` — run-lifecycle emits (`orchestrator.py` start/finish transactions) and
     the normalization `REPORT_IMPORTED` / `PROJECTION_FAILED` emits;
   - `monthly_channel_revenue_facts` — `record_fact` upserts via
     `GoogleSourceNormalizer.normalize_month`;
   - `finance_month_close` — `get_or_create_month_close_row(for_update=True)` can INSERT an
     OPEN row (`google_source_normalizer.py:217-222`).
   Compounding contract break: when the fact INSERT denies,
   `_record_projection_failure_on_run`'s own audit INSERT also denies, the FAILED rewrite
   rolls back, and the durable `connector_runs` row stays **SUCCEEDED with zero facts**.

Verified P3 ride-alongs (same files, fixed here):

3. `ConnectorRunOutcome.analytics_cleanup_blocked` is write-only: the docstrings
   (`orchestrator.py:200-207`, `:846-852`) claim the normalize gate consumes it, but the
   gate read was removed in `a3a584a`. Today's safety is incidental (blocked=True always
   co-occurs with a `per_report_failures` entry) — a phantom guard on the finance projection
   path.
4. Docs drift: `Docs/13_SQL_DATA_MODEL.md:241` still calls `google_revenue_source_rows`
   "planned"; `Docs/12_BACKEND_API_SPEC.md:364-367` still calls fact normalization "future";
   the `/connectors/jobs` no-op contract and the run-driven `REPORT_IMPORTED` /
   `PROJECTION_FAILED` audit semantics are undocumented.

## Design (locked)

### 1. Lane helper — `backend/ums_smart_revenue/db/lane.py`

A context manager generalizing the sanctioned single-session elevation precedent
(`finance/committed_allocation.py:245-288`):

```py
@contextmanager
def platform_lane(session: Session) -> Iterator[None]:
    # No-op off Postgres. On Postgres: ensure the transaction has begun
    # (session.connection() — the after_begin hook fires first and pins the
    # session's configured lane), then SET LOCAL ROLE "app_platform"; on exit,
    # restore "app_tenant" ONLY for tenant-lane sessions (session.info marker),
    # platform-lane sessions stay elevated. SET LOCAL is transaction-scoped, so
    # a commit/rollback inside the block ends the elevation with the
    # transaction — callers must not commit mid-block and keep writing.
```

Atomicity is the point: the run path's facts+audit+commit stay one transaction on one
session (the #90/#93 contract), unlike the API's two-session platform-binding pattern.
`committed_allocation.py` is **not** refactored onto the helper in this PR (merged finance
code, separate review surface); a follow-up note records that.

### 2. Tenant context helper — `backend/ums_smart_revenue/connectors/runs/tenant_context.py`

```py
@contextmanager
def connector_tenant_context(tenant_id: UUID) -> Iterator[None]:
    # TENANT_CTX.set(<minimal Tenant for tenant_id>) ... finally: reset token.
```

Builds the minimal `tenancy` model object the session hook needs (the hook reads only
`tenant.id`). Used by the CLI now, by the executor worker later (per the 2026-06-11
executor spec).

### 3. Wiring (the complete set of platform-only write surfaces in the run path)

- `orchestrator.py` — wrap with `platform_lane(session)`:
  - the `start_run` + `emit_run_started` + commit transaction;
  - the `finish_run` + `emit_run_finished` + commit transactions
    (`_finish_failed_live_run`, `_finish_aggregate_live_run`);
  - the `_sweep_unfinished_live_run` rescue transaction (also emits);
  - dry-run emits if any exist (audit-only; verify during implementation).
- `connectors/runs/normalization.py` — wrap:
  - the normalize block (facts + month-close get-or-create + `REPORT_IMPORTED` emits +
    commit) in `normalize_after_run`;
  - `_record_projection_failure_on_run` (run rewrite is tenant-writable but the
    `PROJECTION_FAILED` emit is not; the whole short transaction elevates).
  The LOCKED prefilter SELECT stays on the tenant lane (read, policy-scoped) — elevation
  starts only where platform-only writes begin.
- Per-report ingest transactions (raw files, source rows, mark_parsed, stale deletes) stay
  **tenant-lane** — all tenant-writable; RLS keeps doing its job there.
- `scripts/run_google_connector.py` — wrap the `run_one` call in
  `connector_tenant_context(args.tenant)`. `FIX:` comment citing the fail-closed-empty gap.

### 4. `analytics_cleanup_blocked` gate restore

Re-consume the flag explicitly in `_normalize_ingested_source_rows` (defense-in-depth, as
both docstrings already promise): a PARTIAL run with `analytics_cleanup_blocked=True` skips
normalize even if `per_report_failures` is empty. Strictly more conservative; pin with a new
wiring test (blocked=True + empty failures → not invoked). Docstrings then match behavior.

### 5. Docs

`Docs/13:241` planned→live wording; `Docs/12` ingestion section rewritten to current
behavior (source-rows API exists; post-run normalization writes facts; run-driven
`REPORT_IMPORTED` with `triggered_by_run_id` + `PROJECTION_FAILED` run-rewrite semantics;
`/connectors/jobs` documented as a recorded no-op pending the executor spec). `Docs/15`
inline status entry for this PR.

## Test obligations (TDD; the PG tests are the heart of this PR)

- **PG-tier (new file, e.g. `tests/connectors/runs/test_run_one_rls_postgres.py`)**, using
  the existing `require_postgres_url` + migrated-to-head harness patterns:
  1. RED first: on a tenant-lane session with `TENANT_CTX` set, the pre-fix lifecycle-emit
     transaction and the pre-fix normalization write set raise permission-denied — written
     and observed failing BEFORE the wiring fix lands (run-to-fail logged in the task).
  2. GREEN: post-fix, a simulated live run (network layer stubbed; DB writes real) drives
     `run_one` end-to-end on Postgres: connector run row SUCCEEDED, source rows present,
     facts projected, `REPORT_IMPORTED` + lifecycle audit rows present — all scoped to the
     context tenant.
  3. Cross-tenant isolation: with tenant A context, rows for tenant B remain invisible
     (read) and unwritable.
  4. CLI context helper: without it, `_load_credential` sees nothing (fail-closed pin);
     with it, the credential row resolves.
  5. Projection-failure path on PG: a forced normalize error rewrites the run FAILED and
     persists the `PROJECTION_FAILED` audit row (the P2 compounding break, now working).
- **SQLite unit tests**: `platform_lane` no-ops off Postgres; restores `app_tenant` for
  tenant-lane sessions on a stubbed PG connection; `connector_tenant_context` sets/resets
  `TENANT_CTX` (exception-safe); the `analytics_cleanup_blocked` gate test above; all
  existing wiring/gate tests stay green untouched.
- **Grant-surface tests untouched** — this PR changes no grants, no policies, no migration.

## Blast-radius review

- **Tables/ORM**: none changed; no migration. New code paths write the same rows through
  the same repositories, on the correct lane.
- **PostgreSQL source of truth**: yes — this PR is what makes that statement true in
  practice for ingestion.
- **Authorization more permissive?** The elevation widens *which DB role* executes
  already-trusted writes inside the orchestrator (service-principal-actor paths that API
  routes already perform via platform-bound sessions). RLS tenant scoping still applies to
  `app_platform` (NOBYPASSRLS + context-row policies). No grant, policy, route, or
  permission change; tenant-lane request code cannot reach the helper's elevation without
  already holding the session object. Fail-closed behaviors (missing context → empty) are
  preserved and now tested on PG.
- **Finance/locks**: locked-month guards unchanged (3 layers re-verified in the #93 review);
  the gate restore in item 4 is strictly more conservative.
- **Neo4j**: No graph projection impact detected (no graph code in backend/ — re-verified
  2026-06-11).
- **Rollback**: code-only revert; no data reset.

## Non-goals

- No executor (next PR, after the 2026-06-11 spec review).
- No grant/policy/migration changes; no credential telemetry; no celery.
- No refactor of `committed_allocation.py` onto the new helper (follow-up note only).
